"""
Settles the weekend (Friday -> Monday) iron condor(s) and records the realized outcome.

Sibling of 0dte_settle_trades.py, adapted for a position whose entry date and expiration
date are NOT the same day. Run this once after the expiration day's market close (e.g.
Monday 4:15pm ET -- or later, if Monday was a holiday and the real expiration fell on
Tuesday). It:

  1. Reads logs/weekend_trades.csv (the entry log written by weekend_iron_condor_bot.py)
     for rows that were actually submitted (not --dry-run, have an order_id) and don't
     yet have a matching row in logs/weekend_trade_outcomes.csv.
  2. Looks up the real order via Alpaca to get its status and the actual filled net
     credit.
  3. Pulls SPY's official close price on the EXPIRATION date (not the entry date).
  4. Computes the settlement value of the 4-leg position at that close price and the
     realized P&L, using the exact strikes logged at entry -- unless the position was
     closed early by weekend_monitor_and_exit.py, in which case it uses the real
     entry_credit vs exit_debit instead (see get_early_close()).
  5. Appends one row per trade to logs/weekend_trade_outcomes.csv.

Fees: unlike the 0DTE bot (where entry and settlement happen the same day, so all real
fee activity is naturally on one date), a weekend hold can generate fee activity on TWO
different dates -- entry-date fees (from opening the 4 legs on e.g. Friday) and a second
batch on whichever date the position's fate was actually decided (the expiration date if
held to expiration -- physically-settled ITM legs can trigger exercise/assignment
activity -- or the early-close date if weekend_monitor_and_exit.py closed it before
expiration). This script queries and sums fees from BOTH relevant dates rather than just
one. See get_fees_for_dates() below.
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
from alpaca_config import ALPACA_PAPER, API_KEY, SECRET_KEY

load_dotenv()
log = get_logger("weekend_settle_trades")

QTY = int(os.getenv("WEEKEND_QTY", "1"))

# See settle_trades.py / 0dte_settle_trades.py for why this must NOT default to "iex".
STOCK_DATA_FEED = DataFeed(os.getenv("STOCK_DATA_FEED", "sip"))

BASE = Path(__file__).parent
LOG_DIR = BASE / "logs"
TRADE_LOG_CSV = LOG_DIR / "weekend_trades.csv"
OUTCOMES_CSV = LOG_DIR / "weekend_trade_outcomes.csv"
CLOSED_EARLY_CSV = LOG_DIR / "weekend_closed_early.csv"

TIMEZONE = ZoneInfo("America/New_York")


def get_clients():
    if not API_KEY or not SECRET_KEY:
        raise RuntimeError(
            "Alpaca API key/secret not set for the active mode (check ALPACA_PAPER and the "
            "matching ALPACA_PAPER_*/ALPACA_LIVE_*/ALPACA_API_KEY/ALPACA_SECRET_KEY vars in .env)."
        )
    if ALPACA_PAPER:
        log.info("Mode: PAPER trading (ALPACA_PAPER=true).")
    else:
        log.warning("!!! LIVE TRADING MODE (ALPACA_PAPER=false) -- real money, real orders !!!")
    trade_client = TradingClient(api_key=API_KEY, secret_key=SECRET_KEY, paper=ALPACA_PAPER)
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
    """Checks logs/weekend_closed_early.csv (written by weekend_monitor_and_exit.py) for
    a completed early close of this order. Returns None if the position was held all the
    way to expiration as normal."""
    for r in read_csv_rows(CLOSED_EARLY_CSV):
        if r.get("order_id") == order_id and r.get("close_status") == "closed":
            return r
    return None


def parse_date_flexible(date_str):
    """See settle_trades.py -- guards against Excel silently reformatting an ISO date
    cell if this CSV is ever opened/saved in Excel."""
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
    """Same sip-with-iex-fallback behavior as settle_trades.py / 0dte_settle_trades.py."""
    day = parse_date_flexible(date_str)

    used_feed = STOCK_DATA_FEED
    try:
        day_bars = _fetch_daily_bar(stock_data_client, symbol, day, STOCK_DATA_FEED)
    except APIError as e:
        if STOCK_DATA_FEED != DataFeed.IEX:
            log.warning(
                f"'{STOCK_DATA_FEED.value}' feed rejected for {date_str} ({e}) -- falling back "
                "to 'iex' for this close price. Re-run with --force --date "
                f"{day.isoformat()} later to upgrade to the more accurate sip close."
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
    """Sums real Alpaca fee activity for a single date. See settle_trades.py's
    get_fees_for_date() docstring for the full rationale on why FEE activity is summed
    for the whole date rather than filtered by symbol (regulatory/exchange fees like OCC
    Clearing, CAT, OPT TAF, ORF, OPT REG are frequently account/day-level, not
    symbol-tagged)."""
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
        atype = a.get("activity_type")
        net = a.get("net_amount")
        if atype == "FEE" and net is not None:
            fee_total += float(net)
            continue

        sym = a.get("symbol")
        if sym not in symbol_set:
            continue
        if atype in ("FILL", "OPTRD"):
            fills.append({
                "symbol": sym, "side": a.get("side"), "qty": a.get("qty"),
                "price": a.get("price"), "transaction_time": a.get("transaction_time"),
            })
        else:
            other_activity.append(f"{atype}(sym={sym}, net_amount={net})")

    return fee_total, other_activity, fills, None


def get_fees_for_dates(trade_client, dates, symbols):
    """Sums fee activity across MULTIPLE dates (deduplicated) -- a weekend hold can
    generate real fee activity on both the entry date (opening the 4 legs) and a
    separate later date (expiration-day exercise/assignment, or an early-close date),
    unlike the 0DTE bot where both always fall on the same day. Returns the combined
    fee_total, combined other_activity/fills lists (each tagged with which date they
    came from), and any error message (fee data for that date is simply skipped, with a
    note, rather than aborting the whole settlement)."""
    unique_dates = sorted(set(dates))
    fee_total = 0.0
    other_activity = []
    fills = []
    errors = []
    for d in unique_dates:
        ft, oa, fl, err = get_fees_for_date(trade_client, d, symbols)
        if err:
            errors.append(f"{d}: {err}")
            continue
        fee_total += ft
        other_activity.extend(f"[{d}] {x}" for x in oa)
        fills.extend({**f, "activity_date": d} for f in fl)
    error_msg = "; ".join(errors) if errors else None
    return fee_total, other_activity, fills, error_msg


def settlement_value(short_put, long_put, short_call, long_call, close):
    return (
        max(0.0, short_put - close) - max(0.0, long_put - close)
        + max(0.0, close - short_call) - max(0.0, close - long_call)
    )


def settle_row(trade_client, stock_data_client, row):
    order_id = row["order_id"]
    order = trade_client.get_order_by_id(order_id)
    status = order.status.value if hasattr(order.status, "value") else str(order.status)

    entry_date = row["date"]
    expiration_date = row.get("expiration_date") or entry_date
    short_put = float(row["short_put_strike"])
    long_put = float(row["long_put_strike"])
    short_call = float(row["short_call_strike"])
    long_call = float(row["long_call_strike"])
    qty = int(row.get("qty", QTY))

    result = {
        "date": entry_date,
        "expiration_date": expiration_date,
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

    if order.filled_avg_price:
        raw_price = float(order.filled_avg_price)
        fill_credit = abs(raw_price)
        if target_credit > 0 and not (0.2 * target_credit <= fill_credit <= 5 * target_credit):
            notes.append(
                f"fill_credit (${fill_credit:.2f}) is far from the target credit logged at entry "
                f"(${target_credit:.2f}) -- verify against the Alpaca dashboard."
            )
    else:
        raw_price = None
        fill_credit = target_credit
        notes.append("order.filled_avg_price was empty -- fell back to the target credit logged at entry.")

    early = get_early_close(order_id)
    close, used_feed, settle_val = None, None, None
    fee_dates = [entry_date]

    if early:
        exit_debit = float(early["exit_debit"])
        early_entry_credit = float(early["entry_credit"])
        if abs(early_entry_credit - fill_credit) > 0.01:
            notes.append(
                f"entry_credit recorded by weekend_monitor_and_exit.py (${early_entry_credit:.2f}) differs "
                f"from fill_credit computed here (${fill_credit:.2f}) -- using the monitor's value."
            )
            fill_credit = early_entry_credit
        gross_pnl = (fill_credit - exit_debit) * 100 * qty
        close_date = early.get("filled_at", "")[:10] or expiration_date
        if close_date != entry_date:
            fee_dates.append(close_date)
        notes.append(
            f"Position was closed EARLY by weekend_monitor_and_exit.py (trigger={early.get('trigger')}, "
            f"filled {early.get('filled_at')}) at exit_debit=${exit_debit:.2f} -- realized_pnl is computed "
            "from entry_credit vs exit_debit, NOT hold-to-expiration settlement."
        )
    else:
        if expiration_date != entry_date:
            fee_dates.append(expiration_date)
        close, used_feed = get_close_price(stock_data_client, row["underlying"], expiration_date)
        if used_feed != STOCK_DATA_FEED:
            notes.append(
                f"used '{used_feed.value}' close instead of '{STOCK_DATA_FEED.value}' (rejected as too "
                f"recent) -- re-run with --force --date {expiration_date} later to upgrade once enough "
                "time has passed."
            )
        settle_val = settlement_value(short_put, long_put, short_call, long_call, close)
        gross_pnl = (fill_credit - settle_val) * 100 * qty

    leg_symbols = [
        row.get("short_put_symbol"), row.get("long_put_symbol"),
        row.get("short_call_symbol"), row.get("long_call_symbol"),
    ]
    fee_total, other_activity, fills, fee_error = get_fees_for_dates(trade_client, fee_dates, leg_symbols)
    if fee_error:
        fees = 0.0
        notes.append(f"Could not pull real fees for some date(s) ({fee_error}) -- realized_pnl below may be missing some fees.")
    else:
        # Same-day-trade split as settle_trades.py, applied per fee_date -- if multiple
        # weekend trades share the same entry (or expiration/close) date, this date's fee
        # total isn't exclusively this trade's, so approximate by splitting evenly among
        # weekend trades sharing that date.
        all_weekend_trades = [
            t for t in read_csv_rows(TRADE_LOG_CSV)
            if t.get("order_id") and str(t.get("dry_run", "")).lower() != "true"
        ]
        n_sharers = max(
            len([t for t in all_weekend_trades if t.get("date") in fee_dates or t.get("expiration_date") in fee_dates]),
            1,
        )
        fees = fee_total / n_sharers if n_sharers > 1 else fee_total
        if n_sharers > 1:
            notes.append(
                f"{n_sharers} weekend trades share fee date(s) {fee_dates} -- Alpaca's fee activity for "
                f"these date(s) (${fee_total:.2f} total) isn't reliably tagged per-symbol, so it was "
                f"split evenly across them (${fees:.2f} each) as an approximation."
            )
        if other_activity:
            notes.append(f"Other non-fill account activity found (not included in fee total): {other_activity}")
        if len(fills) > 4 and not early:
            notes.append(
                f"{len(fills)} fill(s) found across {fee_dates} for these contracts (expected ~4 for a "
                "position simply opened and held to expiration) -- this position was likely closed or "
                "adjusted before expiration, meaning realized_pnl below is probably WRONG. Check your "
                f"Alpaca order history, or whether weekend_monitor_and_exit.py closed it. Fills: {fills}"
            )

    pnl = gross_pnl + fees

    if not early:
        put_wing = short_put - long_put
        call_wing = long_call - short_call
        put_loss = max(0.0, short_put - close) - max(0.0, long_put - close)
        call_loss = max(0.0, close - short_call) - max(0.0, close - long_call)
        if (put_wing > 0 and put_loss >= 0.95 * put_wing) or (call_wing > 0 and call_loss >= 0.95 * call_wing):
            notes.append(
                f"settlement_value (${settle_val:.2f}) looks like a max (or near-max) loss on one side "
                f"-- double check `spy_close` (${close:.2f}) on {expiration_date} against the actual "
                "close on your broker/a quote site before trusting this number."
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
    "date", "expiration_date", "order_id", "status", "raw_filled_avg_price", "fill_credit",
    "spy_close", "settlement_value", "gross_pnl", "fees", "realized_pnl", "notes",
]


def main():
    parser = argparse.ArgumentParser(description="Settle weekend (Friday->Monday) iron condor trades expiring on a given date.")
    parser.add_argument(
        "--date", metavar="YYYY-MM-DD",
        help="Settle trades with this EXPIRATION date (not entry date) instead of all unsettled rows.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-settle rows even if already present in weekend_trade_outcomes.csv, replacing the old row. "
             "Without --date this would re-settle everything -- you'll be asked to confirm.",
    )
    args = parser.parse_args()

    if args.force and not args.date:
        confirm = input(
            "--force without --date will re-settle EVERY previously-settled weekend trade. Continue? [y/N] "
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
        and (args.date is None or r.get("expiration_date") == args.date)
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
                "date": row["date"], "expiration_date": row.get("expiration_date", ""), "order_id": row["order_id"],
                "status": "error", "raw_filled_avg_price": "", "fill_credit": "", "spy_close": "",
                "settlement_value": "", "gross_pnl": "", "fees": "", "realized_pnl": "", "notes": str(e),
            }
        new_results.append(result)
        log.info(f"entry={result['date']} expiration={result['expiration_date']}  order={result['order_id']}  "
                 f"status={result['status']}  pnl={result['realized_pnl'] or 'n/a'}  {result['notes']}")

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
