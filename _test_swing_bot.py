"""Ad-hoc smoke test for swing_iron_condor_bot.py -- the nearest-expiration-to-target-DTE
selection and the no-open-position entry gate, mocked, no live API calls needed."""

import csv
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from alpaca.trading.models import Calendar

import swing_iron_condor_bot as bot
from weekend_time import TIMEZONE


def contract(symbol, strike, expiration_date):
    return SimpleNamespace(symbol=symbol, strike_price=str(strike), expiration_date=expiration_date)


# --- Test 1: get_chain_near_target_dte picks the nearest listed expiration to target ---
spot = 645.0
today = date(2026, 8, 14)  # Friday
target_dte = 45  # -> target_date = 2026-09-28 (a Monday)

# SPY doesn't necessarily list an expiration on the exact target date -- simulate a
# realistic scattering of nearby expirations and confirm it picks the truly nearest one.
exp_far = date(2026, 9, 25)   # 3 days before target (Friday)
exp_near = date(2026, 9, 30)  # 2 days after target (Wednesday) -- should win
exp_farther = date(2026, 10, 2)

calls = (
    [contract(f"SPY{exp_far}C{k}", k, exp_far) for k in range(600, 691)]
    + [contract(f"SPY{exp_near}C{k}", k, exp_near) for k in range(600, 691)]
    + [contract(f"SPY{exp_farther}C{k}", k, exp_farther) for k in range(600, 691)]
)
puts = (
    [contract(f"SPY{exp_far}P{k}", k, exp_far) for k in range(600, 691)]
    + [contract(f"SPY{exp_near}P{k}", k, exp_near) for k in range(600, 691)]
    + [contract(f"SPY{exp_farther}P{k}", k, exp_farther) for k in range(600, 691)]
)

trade_client = MagicMock()
trade_client.get_option_contracts.side_effect = [
    SimpleNamespace(option_contracts=calls),
    SimpleNamespace(option_contracts=puts),
]

sel_calls, sel_puts, chosen_exp = bot.get_chain_near_target_dte(
    trade_client, "SPY", spot, today, target_dte, bot.EXPIRATION_WINDOW_DAYS
)
target_date = today.__class__(2026, 9, 28)
print(f"target_date={target_date}  exp_far={exp_far} (|{(exp_far-target_date).days}|d)  "
      f"exp_near={exp_near} (|{(exp_near-target_date).days}|d)  exp_farther={exp_farther} (|{(exp_farther-target_date).days}|d)")
assert chosen_exp == exp_near, f"expected nearest expiration {exp_near}, got {chosen_exp}"
assert all(c.expiration_date == exp_near for c in sel_calls)
assert all(p.expiration_date == exp_near for p in sel_puts)
print(f"chosen expiration = {chosen_exp} (correctly the nearest to target)")
print("TEST 1 (get_chain_near_target_dte picks nearest expiration) PASSED\n")

# --- Test 2: has_open_position() gating ---
tmp_dir = Path("/tmp/swing_test_logs")
tmp_dir.mkdir(exist_ok=True)
bot.LOG_DIR = tmp_dir
bot.TRADE_LOG_CSV = tmp_dir / "swing_trades.csv"
bot.CLOSED_EARLY_CSV = tmp_dir / "swing_closed_early.csv"
bot.OUTCOMES_CSV = tmp_dir / "swing_trade_outcomes.csv"

# 2a. No trades logged at all -> not open
for p in (bot.TRADE_LOG_CSV, bot.CLOSED_EARLY_CSV, bot.OUTCOMES_CSV):
    if p.exists():
        p.unlink()
open_pos, row = bot.has_open_position()
assert open_pos is False
print("2a. No trades logged -> has_open_position() = False (correct)")

# 2b. One real trade logged, not closed, not settled -> IS open
with open(bot.TRADE_LOG_CSV, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["date", "expiration_date", "target_dte", "timestamp", "underlying", "spot", "iv", "em",
                "short_put_symbol", "short_put_strike", "long_put_symbol", "long_put_strike",
                "short_call_symbol", "short_call_strike", "long_call_symbol", "long_call_strike",
                "net_credit", "max_risk", "qty", "order_id", "dry_run"])
    w.writerow(["2026-08-14", "2026-09-25", "45", "2026-08-14T09:31:00", "SPY", "645.00", "0.15", "10.00",
                "SPYP640", "640", "SPYP630", "630", "SPYC650", "650", "SPYC660", "660",
                "1.50", "850.00", "1", "order-abc-123", "False"])
open_pos, row = bot.has_open_position()
assert open_pos is True and row["order_id"] == "order-abc-123"
print("2b. One open, unsettled trade -> has_open_position() = True (correct)")

# 2c. Same trade, but now marked closed in swing_closed_early.csv -> NOT open anymore
with open(bot.CLOSED_EARLY_CSV, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["date", "order_id", "trigger", "entry_credit", "cost_to_close_at_trigger",
                "profit_pct", "loss_pct", "close_order_id", "close_status", "exit_debit",
                "estimated_pnl", "qty", "escalations", "triggered_at", "filled_at", "notes"])
    w.writerow(["2026-08-14", "order-abc-123", "profit_target", "1.50", "0.75", "0.50", "0.00",
                "close-xyz", "closed", "0.75", "75.00", "1", "0", "2026-08-20T10:00:00",
                "2026-08-20T10:05:00", ""])
open_pos, row = bot.has_open_position()
assert open_pos is False
print("2c. Trade closed early -> has_open_position() = False (correct, new entry allowed)")

# 2d. A dry-run row should never count as "open"
bot.CLOSED_EARLY_CSV.unlink()
with open(bot.TRADE_LOG_CSV, "a", newline="") as f:
    w = csv.writer(f)
    w.writerow(["2026-08-15", "2026-09-26", "45", "2026-08-15T09:31:00", "SPY", "646.00", "0.15", "10.00",
                "SPYP641", "641", "SPYP631", "631", "SPYC651", "651", "SPYC661", "661",
                "1.50", "850.00", "1", "", "True"])
open_pos, row = bot.has_open_position()
# still True because of the FIRST (real) row -- dry-run row should just be ignored, not counted
assert open_pos is True and row["order_id"] == "order-abc-123"
print("2d. Dry-run row ignored, real unsettled row still detected -> has_open_position() = True (correct)")

print("\nALL SWING BOT SMOKE TESTS PASSED")
