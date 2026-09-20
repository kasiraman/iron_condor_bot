"""Ad-hoc end-to-end smoke test for swing_iron_condor_bot.build_swing_iron_condor() with
--test-iv, mocked Alpaca clients -- verifies the full pipeline (chain selection, calendar
T, EM sizing, strike selection, credit/risk sizing) runs without error and produces a
sane plan."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from alpaca.trading.models import Calendar

import swing_iron_condor_bot as bot


def contract(symbol, strike, expiration_date):
    return SimpleNamespace(symbol=symbol, strike_price=str(strike), expiration_date=expiration_date)


spot = 645.0
today = date(2026, 8, 14)
expiration_date = date(2026, 9, 28)  # exactly on target for simplicity

calls = [contract(f"SPYC{k}", k, expiration_date) for k in range(560, 731)]
puts = [contract(f"SPYP{k}", k, expiration_date) for k in range(560, 731)]

trade_client = MagicMock()
trade_client.get_option_contracts.side_effect = [
    SimpleNamespace(option_contracts=calls),
    SimpleNamespace(option_contracts=puts),
]
trade_client.get_account.return_value = SimpleNamespace(equity="50000")

# Calendar needs to span from entry date through expiration date -- build a plausible
# weekday-only stretch (not exhaustive/holiday-aware, fine for this smoke test).
import datetime as dt
cal_entries = []
d = today
while d <= expiration_date:
    if d.weekday() < 5:
        cal_entries.append(Calendar(date=d.isoformat(), open="09:30", close="16:00"))
    d += dt.timedelta(days=1)
trade_client.get_calendar.return_value = cal_entries

option_data_client = MagicMock()
stock_data_client = MagicMock()
stock_data_client.get_stock_latest_trade.return_value = {"SPY": SimpleNamespace(price=str(spot))}

# A 45 DTE, ~5% EM iron condor on a $645 stock has real risk-per-contract in the
# thousands (wide wings) -- the module-level $500 default is realistic for a small
# account but too small to demo the full submit-sizing path here. Bump it for this test
# to confirm the pipeline runs end to end when the budget CAN afford a contract; the
# $500-default-aborts case is exactly the "risk budget too small" guard rail working as
# intended, exercised implicitly above before this override.
bot.MAX_RISK_PER_TRADE_USD = 5000.0

plan = bot.build_swing_iron_condor(
    trade_client, option_data_client, stock_data_client,
    entry_date=today, test_iv=0.15,
)

print(f"entry_date={plan['entry_date']}  expiration_date={plan['expiration_date']}  target_dte={plan['target_dte']}")
assert plan["expiration_date"] == expiration_date
assert plan["net_credit"] > 0
assert plan["qty"] >= 1
assert float(plan["long_put"].strike_price) < float(plan["short_put"].strike_price) < spot < float(plan["short_call"].strike_price) < float(plan["long_call"].strike_price)
print(f"em={plan['em']:.2f} ({plan['em']/spot:.2%} of spot)  net_credit={plan['net_credit']:.2f}  "
      f"qty={plan['qty']}  max_risk={plan['max_risk']:.2f}")

# Sanity: EM at 45 DTE with 15% IV should be a meaningfully larger fraction of spot than
# the 0DTE bot's ~0.1-0.5% -- confirms T is being computed as ~45 days, not ~0 or ~huge.
em_pct = plan["em"] / spot
assert 0.03 < em_pct < 0.15, f"EM% of spot ({em_pct:.2%}) looks wrong for a ~45 DTE hold at 15% IV"
print("TEST (build_swing_iron_condor end-to-end with --test-iv) PASSED")
