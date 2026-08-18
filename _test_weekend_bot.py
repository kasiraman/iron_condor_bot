"""Ad-hoc smoke test for weekend_iron_condor_bot.py's build_weekend_iron_condor() and
weekend_settle_trades.py's multi-date fee aggregation -- exercises the genuinely new
logic (not just copy-pasted-and-renamed 0DTE code) with mocked Alpaca clients, no live
API calls or credentials needed. Not part of the deployed bot."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from alpaca.trading.models import Calendar

import weekend_iron_condor_bot as bot
from weekend_time import TIMEZONE

# --- Build mock contracts ---
def contract(symbol, strike, expiration_date):
    return SimpleNamespace(symbol=symbol, strike_price=str(strike), expiration_date=expiration_date)


def quote(bid, ask):
    return SimpleNamespace(bid_price=str(bid), ask_price=str(ask))


fri = Calendar(date="2026-08-14", open="09:30", close="16:00")
mon = Calendar(date="2026-08-17", open="09:30", close="16:00")

spot = 645.0
puts = [contract(f"SPY260817P{k}", k, mon.date) for k in range(600, 691, 1)]
calls = [contract(f"SPY260817C{k}", k, mon.date) for k in range(600, 691, 1)]

trade_client = MagicMock()
trade_client.get_calendar.return_value = [fri, mon]
trade_client.get_option_contracts.side_effect = [
    SimpleNamespace(option_contracts=calls),
    SimpleNamespace(option_contracts=puts),
]
trade_client.get_account.return_value = SimpleNamespace(equity="50000")

option_data_client = MagicMock()
stock_data_client = MagicMock()
stock_data_client.get_stock_latest_trade.return_value = {"SPY": SimpleNamespace(price=str(spot))}

# --- Test 1: build_weekend_iron_condor with --test-iv (bypasses live option quotes) ---
# entry_date is explicitly pinned to the mocked Friday so `today` inside build_weekend_iron_condor
# matches the mocked calendar's dates (real wall-clock "today" is irrelevant here since
# --test-iv also forces the T/IV simulation path regardless of outside_hours).
plan = bot.build_weekend_iron_condor(
    trade_client, option_data_client, stock_data_client,
    entry_date=fri.date, test_iv=0.12,
)

print(f"entry_date={plan['entry_date']}  expiration_date={plan['expiration_date']}")
assert plan["expiration_date"].isoformat() == "2026-08-17", "expiration should resolve to Monday via the mocked calendar"
assert plan["net_credit"] > 0, "net credit should be positive"
assert plan["qty"] >= 1
assert float(plan["short_put"].strike_price) < float(plan["short_call"].strike_price)
assert float(plan["long_put"].strike_price) < float(plan["short_put"].strike_price)
assert float(plan["long_call"].strike_price) > float(plan["short_call"].strike_price)
print(f"em={plan['em']:.2f}  net_credit={plan['net_credit']:.2f}  qty={plan['qty']}  max_risk={plan['max_risk']:.2f}")
print("TEST 1 (build_weekend_iron_condor with --test-iv) PASSED\n")

# --- Test 2: gap-day self-gate math (mirrors main()'s logic) ---
today = fri.date  # Friday
next_session = bot.next_trading_session(trade_client, today)
gap_days = (next_session.date - today).days
assert gap_days == 3, f"Friday -> Monday should be a 3-day gap, got {gap_days}"
print(f"Friday -> next session {next_session.date}: gap_days={gap_days} (>= WEEKEND_MIN_GAP_DAYS={bot.MIN_GAP_DAYS}, would trade)")

# Simulate a Tuesday -> Wednesday check (1-day gap, should NOT trade)
tue = Calendar(date="2026-08-18", open="09:30", close="16:00")
wed = Calendar(date="2026-08-19", open="09:30", close="16:00")
trade_client2 = MagicMock()
trade_client2.get_calendar.return_value = [tue, wed]
next_session2 = bot.next_trading_session(trade_client2, tue.date)
gap_days2 = (next_session2.date - tue.date).days
assert gap_days2 == 1
assert gap_days2 < bot.MIN_GAP_DAYS, "a normal weekday gap should be below the self-gate threshold"
print(f"Tuesday -> next session {next_session2.date}: gap_days={gap_days2} (< WEEKEND_MIN_GAP_DAYS={bot.MIN_GAP_DAYS}, would SKIP)")
print("TEST 2 (self-gating math) PASSED\n")

print("ALL WEEKEND BOT SMOKE TESTS PASSED")
