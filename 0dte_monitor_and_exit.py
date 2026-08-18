"""
Exit-management monitor for the SPY 0DTE iron condor bot (Alpaca paper/live trading).

NOTE: this is the renamed 0dte_ version of what used to be monitor_and_exit.py -- same
code, same logs/trades.csv and logs/closed_early.csv (unprefixed, kept for continuity
with existing history), just a clearer name now that a second strategy
(weekend_monitor_and_exit.py, for the Friday-to-Monday hold) exists alongside it.

This is a companion script to 0dte_iron_condor_bot.py. It does NOT open positions --
it watches whatever 0dte_iron_condor_bot.py already opened today and closes it early if:

  - profit reaches PROFIT_TARGET_PCT (default 80%) of the credit collected, or
  - loss reaches STOP_LOSS_PCT (default 120%) of the credit collected.

Definitions (both relative to the credit collected at entry, `entry_credit`):
  cost_to_close = live cost to buy back the same 4-leg combo right now
  profit_pct    = (entry_credit - cost_to_close) / entry_credit
  loss_pct      = (cost_to_close - entry_credit) / entry_credit
  -> close when profit_pct >= 0.80  (cost_to_close has fallen to <= 20% of credit)
  -> close when loss_pct   >= 1.20  (cost_to_close has risen to >= 220% of credit)

DESIGN: this script is meant to be invoked repeatedly (e.g. every 1-2 minutes via
cron) during market hours, not run as one long-lived process. Each invocation is
stateless except for logs/closed_early.csv, which it reads and updates -- that's
what makes repeated invocations safe (idempotent): it will never submit a second
closing order for a position it has already closed or already has a pending close
order out for. This mirrors 0dte_iron_condor_bot.py / 0dte_settle_trades.py, which are
also short-lived, cron-invoked scripts rather than daemons.

Integration with 0dte_settle_trades.py: if a position shows up here as "closed" (closed
early, before expiration), 0dte_settle_trades.py should use the actual entry_credit vs
exit_debit recorded here for realized P&L instead of computing hold-to-expiration
settlement value against the close price -- that settlement math is only valid for
positions that were never closed early. See 0dte_settle_trades.py's `get_early_close()`.
"""

import argparse
import csv
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import OptionLegRequest, LimitOrderRequest
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce, OrderStatus
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest
from alpaca.data.enums import OptionsFeed

from bot_logging import get_logger
from alpaca_config import ALPACA_PAPER, API_KEY, SECRET_KEY

load_dotenv()
log = get_logger("0dte_monitor_and_exit")

QTY = int(os.getenv("QTY", "1"))

OPTION_DATA_FEED = OptionsFeed(os.getenv("OPTION_DATA_FEED", "indicative"))

PROFIT_TARGET_PCT = float(os.getenv("PROFIT_TARGET_PCT", "0.80"))   # close at 80% of credit captured
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "1.20"))           # close at 120% loss of credit collected
PROFIT_CLOSE_BUFFER = float(os.getenv("PROFIT_CLOSE_BUFFER", "0.02"))  # pay a bit over fair value to help the fill
STOP_LOSS_BUFFER = float(os.getenv("STOP_LOSS_BUFFER", "0.10"))        # pay more -- urgency matters for a stop
MONITOR_END_TIME = os.getenv("MONITOR_END_TIME", "15:55")  # stop opening NEW closes after this ET time
PENDING_CLOSE_TIMEOUT_MIN = int(os.getenv("PENDING_CLOSE_TIMEOUT_MIN", "10"))
RESUBMIT_BUFFER_STEP = float(os.getenv("RESUBMIT_BUFFER_STEP", "0.05"))
MAX_ESCALATIONS = int(os.getenv("MAX_ESCALATIONS", "5"))

BASE = Path(__file__).parent
LOG_DIR = BASE / "logs"
TRADE_LOG_CSV = LOG_DIR / "trades.csv"  # unprefixed on purpose -- preserves existing trade history
CLOSED_EARLY_CSV = LOG_DIR / "closed_early.csv"  # unprefixed on purpose

TIMEZONE = ZoneInfo("America/New_York")

CLOSED_EARLY_FIELDS = [
    "date", "order_id", "trigger", "entry_credit", "cost_to_close_at_trigger",
    "profit_pct", "loss_pct", "close_order_id", "close_status", "exit_debit",
    "estimated_pnl", "qty", "escalations", "triggered_at", "filled_at", "notes",
]


# --------------------------------------------------------------------------
# Setup / IO helpers
# --------------------------------------------------------------------------
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
    option_data_client = OptionHistoricalDataClient(api_key=API_KEY, secret_key=SECRET_KEY)
    return trade_client, option_data_client


def read_csv_rows(path):
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def write_closed_early(rows):
    LOG_DIR.mkdir(exist_ok=True)
    with open(CLOSED_EARLY_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CLOSED_EARLY_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in CLOSED_EARLY_FIELDS})


def upsert(rows, new_row):
    by_id = {r["order_id"]: r for r in rows}
    by_id[new_row["order_id"]] = new_row
    return list(by_id.values())


def now_iso():
    return datetime.now(TIMEZONE).isoformat()


# --------------------------------------------------------------------------
# Quote / pricing helpers
# --------------------------------------------------------------------------
def get_quotes(option_data_client, symbols):
    req = OptionLatestQuoteRequest(symbol_or_symbols=symbols, feed=OPTION_DATA_FEED)
    return option_data_client.get_option_latest_quote(req)


def cost_to_close(quotes, short_put_sym, long_put_sym, short_call_sym, long_call_sym):
    """Conservative (worst-case, guaranteed-fillable) cost to buy back the combo right
    now: buy back the shorts at their ask, sell the longs at their bid. This mirrors
    0dte_iron_condor_bot.py's entry credit calc (shorts at bid, longs at ask) with the
    bid/ask roles reversed, since closing reverses every leg's side."""
    return (
        float(quotes[short_put_sym].ask_price)
        + float(quotes[short_call_sym].ask_price)
        - float(quotes[long_put_sym].bid_price)
        - float(quotes[long_call_sym].bid_price)
    )


def get_entry_credit(trade_client, row):
    """Same abs()-based sign fix used in 0dte_settle_trades.py: this strategy only ever
    SELLS the iron condor, so the real credit is always positive by construction,
    regardless of how Alpaca's filled_avg_price sign convention reports it."""
    order = trade_client.get_order_by_id(row["order_id"])
    status = order.status.value if hasattr(order.status, "value") else str(order.status)
    if status != OrderStatus.FILLED.value:
        return None, status
    if order.filled_avg_price:
        return abs(float(order.filled_avg_price)), status
    return float(row["net_credit"]), status


# --------------------------------------------------------------------------
# Order submission
# --------------------------------------------------------------------------
def submit_closing_order(trade_client, row, limit_price):
    qty = int(row.get("qty", QTY))
    legs = [
        OptionLegRequest(symbol=row["short_put_symbol"], side=OrderSide.BUY, ratio_qty=1),
        OptionLegRequest(symbol=row["long_put_symbol"], side=OrderSide.SELL, ratio_qty=1),
        OptionLegRequest(symbol=row["short_call_symbol"], side=OrderSide.BUY, ratio_qty=1),
        OptionLegRequest(symbol=row["long_call_symbol"], side=OrderSide.SELL, ratio_qty=1),
    ]
    req = LimitOrderRequest(
        qty=qty,
        order_class=OrderClass.MLEG,
        time_in_force=TimeInForce.DAY,
        limit_price=round(max(limit_price, 0.01), 2),
        legs=legs,
    )
    return trade_client.submit_order(req)


# --------------------------------------------------------------------------
# Per-position evaluation
# --------------------------------------------------------------------------
def evaluate_new_position(trade_client, option_data_client, row, past_cutoff, dry_run):
    order_id = row["order_id"]
    entry_credit, entry_status = get_entry_credit(trade_client, row)
    if entry_credit is None:
        log.info(f"{row['date']}  order={order_id}  entry order status={entry_status} -- not filled yet, nothing to monitor.")
        return None

    leg_syms = {
        "short_put": row["short_put_symbol"], "long_put": row["long_put_symbol"],
        "short_call": row["short_call_symbol"], "long_call": row["long_call_symbol"],
    }
    quotes = get_quotes(option_data_client, list(leg_syms.values()))
    ctc = cost_to_close(quotes, leg_syms["short_put"], leg_syms["long_put"],
                         leg_syms["short_call"], leg_syms["long_call"])

    profit_pct = (entry_credit - ctc) / entry_credit if entry_credit else 0.0
    loss_pct = (ctc - entry_credit) / entry_credit if entry_credit else 0.0

    trigger, limit_price = None, None
    if profit_pct >= PROFIT_TARGET_PCT:
        trigger, limit_price = "profit_target", ctc + PROFIT_CLOSE_BUFFER
    elif loss_pct >= STOP_LOSS_PCT:
        trigger, limit_price = "stop_loss", ctc + STOP_LOSS_BUFFER

    log.info(
        f"{row['date']}  order={order_id}  entry_credit={entry_credit:.2f}  "
        f"cost_to_close={ctc:.2f}  profit_pct={profit_pct:.1%}  loss_pct={loss_pct:.1%}"
        + (f"  -> TRIGGER {trigger}" if trigger else "  -> no action")
    )

    if not trigger:
        return None

    if past_cutoff:
        log.info(f"  (past MONITOR_END_TIME={MONITOR_END_TIME} -- skipping new close, letting 0dte_settle_trades.py handle it at expiration)")
        return None

    if dry_run:
        log.info(f"  [DRY RUN] would submit closing order at limit_price={limit_price:.2f}")
        return None

    close_order = submit_closing_order(trade_client, row, limit_price)
    log.info(f"  Submitted closing order id={close_order.id} limit_price={limit_price:.2f}")

    return {
        "date": row["date"], "order_id": order_id, "trigger": trigger,
        "entry_credit": f"{entry_credit:.2f}", "cost_to_close_at_trigger": f"{ctc:.2f}",
        "profit_pct": f"{profit_pct:.4f}", "loss_pct": f"{loss_pct:.4f}",
        "close_order_id": str(close_order.id), "close_status": "pending_close",
        "exit_debit": "", "estimated_pnl": "", "qty": row.get("qty", QTY),
        "escalations": "0", "triggered_at": now_iso(), "filled_at": "", "notes": "",
    }


def check_pending_close(trade_client, existing):
    close_order = trade_client.get_order_by_id(existing["close_order_id"])
    status = close_order.status.value if hasattr(close_order.status, "value") else str(close_order.status)

    if status == OrderStatus.FILLED.value:
        exit_debit = abs(float(close_order.filled_avg_price)) if close_order.filled_avg_price else float(existing["cost_to_close_at_trigger"])
        qty = int(existing.get("qty") or QTY)
        entry_credit = float(existing["entry_credit"])
        estimated_pnl = (entry_credit - exit_debit) * 100 * qty
        existing["close_status"] = "closed"
        existing["exit_debit"] = f"{exit_debit:.2f}"
        existing["estimated_pnl"] = f"{estimated_pnl:.2f}"
        existing["filled_at"] = now_iso()
        log.info(f"{existing['date']}  order={existing['order_id']}  close order FILLED  exit_debit={exit_debit:.2f}  estimated_pnl=${estimated_pnl:.2f}")
        return existing, False

    if status in (OrderStatus.CANCELED.value, OrderStatus.REJECTED.value, OrderStatus.EXPIRED.value):
        log.warning(f"{existing['date']}  order={existing['order_id']}  close order {status} -- will re-evaluate fresh next run.")
        return existing, True  # signal: drop this row, re-evaluate from scratch next run

    # Still open (new/pending_new/accepted/partially_filled). Escalate if it's been
    # sitting too long -- important for the stop-loss case, where a passive limit
    # that never fills defeats the whole point of a stop.
    triggered_at = datetime.fromisoformat(existing["triggered_at"])
    age_min = (datetime.now(TIMEZONE) - triggered_at).total_seconds() / 60.0
    escalations = int(existing.get("escalations") or 0)

    if age_min >= PENDING_CLOSE_TIMEOUT_MIN and escalations < MAX_ESCALATIONS:
        try:
            trade_client.cancel_order_by_id(existing["close_order_id"])
        except Exception as e:
            log.error(f"  Could not cancel stale close order {existing['close_order_id']}: {e}")
            return existing, False
        new_limit = float(existing["cost_to_close_at_trigger"]) + (escalations + 1) * RESUBMIT_BUFFER_STEP
        # Need the original row's leg symbols to resubmit -- look them up from trades.csv.
        trades = read_csv_rows(TRADE_LOG_CSV)
        row = next((r for r in trades if r["order_id"] == existing["order_id"]), None)
        if row is None:
            log.error(f"  Could not find original trade row for order {existing['order_id']} to resubmit -- leaving unclosed, check manually.")
            return existing, False
        new_order = submit_closing_order(trade_client, row, new_limit)
        log.warning(
            f"{existing['date']}  order={existing['order_id']}  close order stale ({age_min:.1f}m, no fill) -- "
            f"canceled and resubmitted id={new_order.id} at limit_price={new_limit:.2f} (escalation #{escalations + 1})"
        )
        existing["close_order_id"] = str(new_order.id)
        existing["escalations"] = str(escalations + 1)
        existing["triggered_at"] = now_iso()
        return existing, False

    if age_min >= PENDING_CLOSE_TIMEOUT_MIN:
        log.error(
            f"{existing['date']}  order={existing['order_id']}  close order still unfilled after "
            f"{MAX_ESCALATIONS} escalation(s) -- giving up on automated escalation, CHECK MANUALLY."
        )

    return existing, False


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Exit-management monitor for open 0DTE iron condor positions.")
    parser.add_argument("--force", action="store_true", help="Skip the market-open check (for manual testing).")
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="Monitor a specific logged date instead of today.")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate and print what would happen, but never submit orders.")
    args = parser.parse_args()

    trade_client, option_data_client = get_clients()

    if not args.force:
        clock = trade_client.get_clock()
        if not clock.is_open:
            log.info("Market is not open right now -- exiting. Use --force to override for testing.")
            return

    target_date = args.date or datetime.now(TIMEZONE).date().isoformat()

    now = datetime.now(TIMEZONE)
    cutoff_h, cutoff_m = (int(x) for x in MONITOR_END_TIME.split(":"))
    past_cutoff = now >= now.replace(hour=cutoff_h, minute=cutoff_m, second=0, microsecond=0)

    trades = read_csv_rows(TRADE_LOG_CSV)
    candidates = [
        r for r in trades
        if r["date"] == target_date and r.get("order_id") and str(r.get("dry_run", "")).lower() != "true"
    ]
    if not candidates:
        log.info(f"No open (non-dry-run) positions logged for {target_date}.")
        return

    closed_rows = read_csv_rows(CLOSED_EARLY_CSV)
    closed_by_id = {r["order_id"]: r for r in closed_rows}
    updated_rows = list(closed_rows)

    for row in candidates:
        order_id = row["order_id"]
        existing = closed_by_id.get(order_id)

        if existing and existing.get("close_status") == "closed":
            continue  # already closed early -- nothing more to do

        if existing and existing.get("close_status") == "pending_close":
            updated, drop = check_pending_close(trade_client, existing)
            if drop:
                updated_rows = [r for r in updated_rows if r["order_id"] != order_id]
            else:
                updated_rows = upsert(updated_rows, updated)
            continue

        new_row = evaluate_new_position(trade_client, option_data_client, row, past_cutoff, args.dry_run)
        if new_row:
            updated_rows = upsert(updated_rows, new_row)

    if not args.dry_run:
        write_closed_early(updated_rows)


if __name__ == "__main__":
    main()
