"""
Settles the day's 0DTE iron condor(s) and records the realized outcome.

Run this once after market close (e.g. 4:15pm ET), after iron_condor_bot.py has
already run at the open. It:

  1. Reads logs/trades.csv (the entry log written by iron_condor_bot.py) for rows
     that were actually submitted (not --dry-run, have an order_id) and don't yet
     have a matching row in logs/trade_outcomes.csv.
  2. Looks up the real order via Alpaca to get its status and the actual filled
     net credit (which may differ from the target credit logged at entry time,
     since the live order fills at whatever price the market gave it).
  3. Pulls SPY's official close price for that date.
  4. Computes the settlement value of the 4-leg position at that close price and
     the realized P&L, using the exact strikes logged at entry.
  5. Appends one row per trade to logs/trade_outcomes.csv.

Since SPY options are physically settled and 0DTE contracts are worthless or
fully in-the-money by end of day, this computes the settlement value directly
from strikes vs. close price rather than relying on Alpaca to report a closed
P&L — that keeps it correct even if Alpaca's own position/activity reporting
for expired contracts lags or differs.
"""

import argparse
import csv
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderStatus
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed
from alpaca.common.exceptions import APIError

from bot_logging import get_logger

load_dotenv()
log = get_logger("settle_trades")

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
QTY = int(os.getenv("QTY", "1"))

# IMPORTANT: this must NOT default to "iex" the way iron_condor_bot.py's live spot-price
# lookup does. IEX only reflects trades that happened on the IEX exchange -- a small
# slice of total volume -- so IEX's "last trade of the day" is often noticeably different
# from the real, official closing price (which comes from the primary exchange's closing
# auction via the consolidated SIP tape). Using IEX here was silently feeding a wrong
# close price into the settlement math, which can make a real partial loss look like a
# full max-loss (or vice versa) even though no error is raised.
#
# "delayed_sip" is NOT valid here -- it turns out that feed value only applies to
# "latest quote/trade" and streaming requests, not historical bar requests, and Alpaca
# rejects it outright ("invalid feed: delayed_sip"). For historical bars, free/basic
# accounts CAN use plain "sip" -- but only if the requested time range doesn't reach into
# the last 15 minutes (i.e. isn't "recent"). See get_close_price() below for how that's
# enforced. Override via .env (STOCK_DATA_FEED=iex) only if sip still fails for your
# account for some reason -- iex will work but may be a few cents off the true close.
STOCK_DATA_FEED = DataFeed(os.getenv("STOCK_DATA_FEED", "sip"))

BASE = Path(__file__).parent
LOG_DIR = BASE / "logs"
TRADE_LOG_CSV = LOG_DIR / "trades.csv"
OUTCOMES_CSV = LOG_DIR / "trade_outcomes.csv"
CLOSED_EARLY_CSV = LOG_DIR / "closed_early.csv"

TIMEZONE = ZoneInfo("America/New_York")


def get_clients():
    if not API_KEY or not SECRET_KEY:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set (check your .env file).")
    trade_client = TradingClient(api_key=API_KEY, secret_key=SECRET_KEY, paper=True)
    stock_data_client = StockHistoricalDataClient(api_key=API_KEY, secret_key=SECRET_KEY)
    return trade_client, stock_data_client


def read_csv_rows(path):
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def already_settled_order_ids(outcomes_rows):
    return {r["order_id"] for r in outcomes_rows}


def get_early_close(order_id):
    """Checks logs/closed_early.csv (written by monitor_and_exit.py) for a completed
    early close of this order. If found, the position was bought back before
    expiration -- the hold-to-expiration settlement math in settle_row() below is
    not valid for it (the real economics are entry_credit vs exit_debit from the
    actual closing trade, not close-price-vs-strikes). Returns None if the position
    was held to expiration as normal (the common case when monitor_and_exit.py never
    triggers)."""
    for r in read_csv_rows(CLOSED_EARLY_CSV):
        if r.get("order_id") == order_id and r.get("close_status") == "closed":
            return r
    return None


def parse_date_flexible(date_str):
    """trades.csv dates are written as ISO (YYYY-MM-DD), but if the CSV is ever opened
    and saved in Excel, Excel silently reformats date-looking cells to your locale's
    format (e.g. M/D/YYYY) -- which is exactly what happened to one row here. Rather
    than crash on that, try ISO first and fall back to the common spreadsheet formats.
    Avoid opening/saving these CSVs in Excel going forward if you can (or import the
    date column as Text) to stop this from recurring."""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Could not parse date '{date_str}' in any known format.")


def _fetch_daily_bar(stock_data_client, symbol, day, feed):
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=day,
        end=day + timedelta(days=1),
        feed=feed,
    )
    bars = stock_data_client.get_stock_bars(req)
    return bars[symbol]


def get_close_price(stock_data_client, symbol, date_str):
    """Free/basic Alpaca accounts CAN use the "sip" feed for historical bars (the real,
    official close) -- but apparently only once the requested day is far enough in the
    past; querying the same afternoon shortly after close can still get rejected as
    "recent" data even though the trading day itself is already final. Rather than try
    to reverse-engineer Alpaca's exact undocumented cutoff, this tries "sip" first (best
    accuracy) and automatically falls back to "iex" if Alpaca rejects it -- so same-day
    settlement still works today, and you'll transparently get the more accurate sip
    price once you re-run/backfill later after enough time has passed."""
    day = parse_date_flexible(date_str)

    used_feed = STOCK_DATA_FEED
    try:
        day_bars = _fetch_daily_bar(stock_data_client, symbol, day, STOCK_DATA_FEED)
    except APIError as e:
        if STOCK_DATA_FEED != DataFeed.IEX:
            log.warning(
                f"'{STOCK_DATA_FEED.value}' feed rejected for {date_str} ({e}) -- falling back "
                "to 'iex' for this close price. Re-run with --force --date "
                f"{day.isoformat()} later (e.g. tomorrow or after) to pick up the more accurate sip close."
            )
            used_feed = DataFeed.IEX
            day_bars = _fetch_daily_bar(stock_data_client, symbol, day, DataFeed.IEX)
        else:
            raise

    if not day_bars:
        raise RuntimeError(
            f"No daily bar found for {symbol} on {date_str} -- either the market hasn't closed "
            "yet, or this date is a non-trading day."
        )
    return float(day_bars[-1].close), used_feed


def get_fees_for_date(trade_client, date_str, symbols):
    """Pulls Alpaca's actual account activity for this date and sums up real fee line
    items (regulatory/exchange/OCC fees etc.), rather than guessing at a fee schedule --
    Alpaca's per-contract options fees aren't something this bot should hardcode (they
    can differ by account or change over time). This is a real, documented Alpaca
    endpoint (GET /v2/account/activities) but alpaca-py doesn't wrap it with a named
    method in the installed version, so it's called directly via trade_client.get().

    Only activity_type == "FEE" is summed into `fee_total` -- that's unambiguously a fee,
    not a trade or a settlement event. Other symbol-matching activity on this date (e.g.
    OPASN/OPEXC for assignment/exercise, which happens because SPY is physically settled)
    is returned separately in `other_activity` so you can see it and decide whether it
    explains any remaining gap versus your Alpaca dashboard -- assignment/exercise events
    can carry their own costs beyond a flat per-contract fee.
    """
    try:
        activities = trade_client.get("/account/activities", data={"date": date_str})
    except Exception as e:
        return None, [], [], f"Could not fetch account activities for {date_str}: {e}"

    if not isinstance(activities, list):
        return None, [], [], f"Unexpected /account/activities response shape for {date_str}: {type(activities)}"

    symbol_set = set(symbols)
    fee_total = 0.0
    other_activity = []
    fills = []
    for a in activities:
        sym = a.get("symbol")
        if sym not in symbol_set:
            continue
        atype = a.get("activity_type")
        net = a.get("net_amount")
        if atype == "FEE" and net is not None:
            fee_total += float(net)
        elif atype in ("FILL", "OPTRD"):
            # Surfaced (not silently dropped) so settle_row can flag if there are MORE
            # fills than a simple "opened 4 legs, held to expiration" position should
            # have -- extra fills mean the position was closed/adjusted before
            # expiration, which would make the hold-to-expiration settlement math below
            # wrong for this trade (the real P&L comes from that closing trade instead).
            fills.append({
                "symbol": sym, "side": a.get("side"), "qty": a.get("qty"),
                "price": a.get("price"), "transaction_time": a.get("transaction_time"),
            })
        else:
            other_activity.append(f"{atype}(sym={sym}, net_amount={net})")

    return fee_total, other_activity, fills, None


def settlement_value(short_put, long_put, short_call, long_call, close):
    return (
        max(0.0, short_put - close) - max(0.0, long_put - close)
        + max(0.0, close - short_call) - max(0.0, close - long_call)
    )


def settle_row(trade_client, stock_data_client, row):
    order_id = row["order_id"]
    order = trade_client.get_order_by_id(order_id)
    status = order.status.value if hasattr(order.status, "value") else str(order.status)

    short_put = float(row["short_put_strike"])
    long_put = float(row["long_put_strike"])
    short_call = float(row["short_call_strike"])
    long_call = float(row["long_call_strike"])
    qty = int(row.get("qty", QTY))

    result = {
        "date": row["date"],
        "order_id": order_id,
        "status": status,
        "raw_filled_avg_price": "",
        "fill_credit": "",
        "spy_close": "",
        "settlement_value": "",
        "gross_pnl": "",
        "fees": "",
        "realized_pnl": "",
        "notes": "",
    }

    if status != OrderStatus.FILLED.value:
        result["notes"] = "order not filled — no position opened, excluded from P&L"
        return result

    target_credit = float(row["net_credit"])
    notes = []

    # This bot only ever SELLS the iron condor (collects a credit) -- it never buys one.
    # So the real, realized credit is always a positive number by construction here.
    # Alpaca's filled_avg_price on a multi-leg order can come back with either sign
    # depending on account/venue conventions (some report credits as negative "cost"),
    # which was previously being subtracted straight through and silently turning every
    # profitable trade into an apparent loss. Taking the magnitude fixes that for this
    # credit-only strategy. raw_filled_avg_price is logged alongside it so you can verify
    # the sign directly against a real fill in your Alpaca dashboard.
    if order.filled_avg_price:
        raw_price = float(order.filled_avg_price)
        fill_credit = abs(raw_price)
        if target_credit > 0 and not (0.2 * target_credit <= fill_credit <= 5 * target_credit):
            notes.append(
                f"fill_credit (${fill_credit:.2f}) is far from the target credit logged at entry "
                f"(${target_credit:.2f}) -- verify against the Alpaca dashboard, something may be off."
            )
    else:
        raw_price = None
        fill_credit = target_credit
        notes.append("order.filled_avg_price was empty -- fell back to the target credit logged at entry.")

    early = get_early_close(order_id)
    close, used_feed, settle_val = None, None, None

    if early:
        exit_debit = float(early["exit_debit"])
        early_entry_credit = float(early["entry_credit"])
        if abs(early_entry_credit - fill_credit) > 0.01:
            notes.append(
                f"entry_credit recorded by monitor_and_exit.py (${early_entry_credit:.2f}) differs from "
                f"fill_credit computed here (${fill_credit:.2f}) -- using the monitor's value since it's "
                "tied directly to the actual closing trade."
            )
            fill_credit = early_entry_credit
        gross_pnl = (fill_credit - exit_debit) * 100 * qty
        notes.append(
            f"Position was closed EARLY by monitor_and_exit.py (trigger={early.get('trigger')}, "
            f"filled {early.get('filled_at')}) at exit_debit=${exit_debit:.2f} -- realized_pnl is computed "
            "from entry_credit vs exit_debit, NOT hold-to-expiration settlement (spy_close/settlement_value "
            "below are not applicable for this trade)."
        )
    else:
        close, used_feed = get_close_price(stock_data_client, row["underlying"], row["date"])
        if used_feed != STOCK_DATA_FEED:
            notes.append(
                f"used '{used_feed.value}' close instead of '{STOCK_DATA_FEED.value}' (rejected as too "
                f"recent) -- re-run with --force --date {row['date']} later to upgrade to the more "
                "accurate close once enough time has passed."
            )
        settle_val = settlement_value(short_put, long_put, short_call, long_call, close)
        gross_pnl = (fill_credit - settle_val) * 100 * qty

    # Alpaca's dashboard P&L includes real per-contract regulatory/exchange fees, which
    # this bot's own math above doesn't know about (and shouldn't guess at -- fee
    # schedules vary and change). Pull the real fee activity for this date instead.
    leg_symbols = [
        row.get("short_put_symbol"), row.get("long_put_symbol"),
        row.get("short_call_symbol"), row.get("long_call_symbol"),
    ]
    fee_total, other_activity, fills, fee_error = get_fees_for_date(trade_client, row["date"], leg_symbols)
    if fee_error:
        fees = 0.0
        notes.append(f"Could not pull real fees ({fee_error}) -- realized_pnl below excludes fees.")
    else:
        fees = fee_total
        if other_activity:
            notes.append(
                "Other non-fill account activity found for these contracts this date (not "
                f"included in the fee total, check if it explains any remaining gap): {other_activity}"
            )
        # A position opened as 4 legs and held to expiration should show ~4 fills total
        # (one per leg, from the single opening order) and nothing else. More than that
        # means an additional trade happened on these contracts that day -- almost
        # certainly the position was closed/adjusted before expiration -- and the
        # hold-to-expiration settlement math this script does is WRONG for that trade;
        # the real P&L is whatever that closing trade actually priced, not the close-vs-
        # strikes math below. This is very likely if realized_pnl doesn't match your
        # broker despite fees and close price both checking out.
        if len(fills) > 4 and not early:
            notes.append(
                f"{len(fills)} fill(s) found for these contracts on this date (expected ~4 for a "
                "position simply opened and held to expiration) -- this position was likely closed "
                "or adjusted before expiration, which means realized_pnl below (computed as if held "
                "to expiration) is probably WRONG. Check your Alpaca order history for this date for "
                f"a closing trade instead, or check whether monitor_and_exit.py closed it (if so, "
                "logs/closed_early.csv should have picked it up automatically). Fills seen: {fills}"
            )
        # Note: when `early` is set, >4 fills is EXPECTED (entry + close) and already
        # explained above -- no separate warning needed.
    pnl = gross_pnl + fees  # fee net_amount is expected to already be a negative (outflow) number

    # If either side's loss is at (or essentially at) that side's own wing width, the close
    # price used here fell beyond BOTH strikes on that side -- true max loss for that side.
    # That should be uncommon; when it happens, it's worth a second look at whether `close`
    # is actually right (e.g. a bad/stale data feed) before trusting the number, since a
    # wrong close can silently turn a small partial loss into an apparent max loss. Checked
    # per side (not against the combined settlement_value) since wing widths can differ
    # slightly between the put and call side after rounding to real strikes.
    # Not applicable to early closes -- there's no `close`/`settle_val` to sanity check there,
    # the real exit price already came from an actual fill.
    if not early:
        put_wing = short_put - long_put
        call_wing = long_call - short_call
        put_loss = max(0.0, short_put - close) - max(0.0, long_put - close)
        call_loss = max(0.0, close - short_call) - max(0.0, close - long_call)
        if (put_wing > 0 and put_loss >= 0.95 * put_wing) or (call_wing > 0 and call_loss >= 0.95 * call_wing):
            notes.append(
                f"settlement_value (${settle_val:.2f}) looks like a max (or near-max) loss on one side "
                f"-- double check `spy_close` (${close:.2f}) against the actual close on your broker/a "
                "quote site before trusting this number; a wrong close price here (e.g. from a data "
                "feed that isn't the official consolidated close) can make a small real loss look like "
                "a max loss."
            )

    result.update({
        "raw_filled_avg_price": f"{raw_price:.4f}" if raw_price is not None else "",
        "fill_credit": f"{fill_credit:.2f}",
        "spy_close": f"{close:.2f}" if close is not None else "",
        "settlement_value": f"{settle_val:.2f}" if settle_val is not None else "",
        "gross_pnl": f"{gross_pnl:.2f}",
        "fees": f"{fees:.2f}",
        "realized_pnl": f"{pnl:.2f}",
        "notes": "; ".join(notes),
    })
    return result


OUTCOME_FIELDS = [
    "date", "order_id", "status", "raw_filled_avg_price", "fill_credit", "spy_close",
    "settlement_value", "gross_pnl", "fees", "realized_pnl", "notes",
]


def main():
    parser = argparse.ArgumentParser(description="Settle today's (or a given date's) 0DTE iron condor trades.")
    parser.add_argument("--date", help="Settle a specific date (YYYY-MM-DD) instead of all unsettled rows.")
    parser.add_argument(
        "--force", action="store_true",
        help="Re-settle rows even if already present in trade_outcomes.csv, replacing the old row. "
             "Use this with --date to upgrade a trade that fell back to the 'iex' close (see its "
             "notes) to the more accurate 'sip' close once enough time has passed. Without --date "
             "this would re-settle everything -- you'll be asked to confirm.",
    )
    args = parser.parse_args()

    if args.force and not args.date:
        confirm = input(
            "--force without --date will re-settle EVERY previously-settled trade. Continue? [y/N] "
        )
        if confirm.strip().lower() != "y":
            log.info("Aborted.")
            return

    trade_client, stock_data_client = get_clients()

    trades = read_csv_rows(TRADE_LOG_CSV)
    outcomes = read_csv_rows(OUTCOMES_CSV)
    settled_ids = already_settled_order_ids(outcomes)

    candidates = [
        r for r in trades
        if r.get("order_id") and str(r.get("dry_run", "")).lower() != "true"
        and (args.date is None or r["date"] == args.date)
    ]
    pending = candidates if args.force else [r for r in candidates if r["order_id"] not in settled_ids]

    if not pending:
        log.info("Nothing to settle.")
        return

    reprocessed_ids = {r["order_id"] for r in pending} if args.force else set()
    kept_outcomes = [o for o in outcomes if o["order_id"] not in reprocessed_ids]

    new_results = []
    for row in pending:
        try:
            result = settle_row(trade_client, stock_data_client, row)
        except Exception as e:
            result = {
                "date": row["date"], "order_id": row["order_id"], "status": "error",
                "raw_filled_avg_price": "", "fill_credit": "", "spy_close": "", "settlement_value": "",
                "gross_pnl": "", "fees": "", "realized_pnl": "", "notes": str(e),
            }
        new_results.append(result)
        log.info(f"{result['date']}  order={result['order_id']}  status={result['status']}  "
                 f"pnl={result['realized_pnl'] or 'n/a'}  {result['notes']}")

    LOG_DIR.mkdir(exist_ok=True)
    with open(OUTCOMES_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTCOME_FIELDS)
        writer.writeheader()
        for o in kept_outcomes:
            writer.writerow({k: o.get(k, "") for k in OUTCOME_FIELDS})
        for r in new_results:
            writer.writerow(r)


if __name__ == "__main__":
    main()
