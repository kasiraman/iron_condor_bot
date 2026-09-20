"""Ad-hoc smoke test for weekend_iron_condor_bot.estimate_weekend_gap_em() -- the new
historical weekend/holiday gap-risk buffer. Uses synthetic daily bars with a KNOWN gap
return distribution so the recovered stdev can be checked against the true input, plus
edge cases (too few samples, disabled flag, feed fallback). No live API calls needed."""

import random
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import weekend_iron_condor_bot as bot

random.seed(42)

SPOT = 645.0
TRUE_GAP_STDEV = 0.01  # 1% true weekend-gap return stdev, known by construction


def bar(d, open_, close_):
    return SimpleNamespace(timestamp=datetime(d.year, d.month, d.day), open=open_, close=close_)


def build_synthetic_bars(n_weekends, gap_stdev=TRUE_GAP_STDEV, gap_days=3, start=date(2023, 1, 2)):
    """Builds a Mon-Fri trading week pattern for n_weekends consecutive weeks, injecting a
    KNOWN-distribution gap return (mean 0, stdev gap_stdev) at each Friday-close ->
    Monday-open boundary (or a longer gap if gap_days > 3, simulating a holiday week)."""
    bars = []
    price = 100.0
    d = start
    for week in range(n_weekends):
        week_start = d + timedelta(days=7 * week)
        prev_close = price
        for wd in range(5):  # Mon-Fri
            day = week_start + timedelta(days=wd)
            if wd == 0 and week > 0:
                # Monday open = prior Friday's close * (1 + synthetic gap return)
                gap_return = random.gauss(0, gap_stdev)
                open_ = prev_close * (1 + gap_return)
            else:
                open_ = prev_close * (1 + random.gauss(0, 0.002))  # tiny regular intraday noise, no gap
            close_ = open_ * (1 + random.gauss(0, 0.003))
            bars.append(bar(day, open_, close_))
            prev_close = close_
        price = prev_close
    return bars


# --- Test 1: recovers the known gap stdev within statistical tolerance ---
bars = build_synthetic_bars(n_weekends=104)  # ~2 years of weekly data -> 103 weekend gaps
trade_client_stock = MagicMock()
trade_client_stock.get_stock_bars.return_value = {"SPY": bars}

gap_em, n = bot.estimate_weekend_gap_em(trade_client_stock, "SPY", SPOT, gap_calendar_days=3)
recovered_stdev = gap_em / SPOT
print(f"n_samples={n}  true_stdev={TRUE_GAP_STDEV:.4f}  recovered_stdev={recovered_stdev:.4f}  "
      f"gap_em=${gap_em:.2f} ({recovered_stdev:.2%} of spot)")
assert n >= 100, f"expected ~103 weekend gap samples, got {n}"
# Sampling error of a stdev estimate from n samples is roughly stdev/sqrt(2n) -- with
# n~103 that's ~7%; allow a generous 30% relative tolerance to avoid test flakiness.
assert abs(recovered_stdev - TRUE_GAP_STDEV) / TRUE_GAP_STDEV < 0.30, "recovered stdev too far from the true input stdev"
print("TEST 1 (recovers known gap stdev) PASSED\n")

# --- Test 2: scaling by gap_calendar_days (a 5-day holiday gap vs a 3-day weekend) ---
gap_em_3, _ = bot.estimate_weekend_gap_em(trade_client_stock, "SPY", SPOT, gap_calendar_days=3)
gap_em_5, _ = bot.estimate_weekend_gap_em(trade_client_stock, "SPY", SPOT, gap_calendar_days=5)
expected_ratio = (5 / 3) ** 0.5
actual_ratio = gap_em_5 / gap_em_3
print(f"gap_em(3d)={gap_em_3:.2f}  gap_em(5d)={gap_em_5:.2f}  ratio={actual_ratio:.4f}  expected~{expected_ratio:.4f}")
assert abs(actual_ratio - expected_ratio) < 0.01, "gap EM should scale with sqrt(gap_calendar_days)"
print("TEST 2 (scales with sqrt(gap_calendar_days)) PASSED\n")

# --- Test 3: too few samples -> falls back to (0.0, n) ---
few_bars = build_synthetic_bars(n_weekends=5)  # only ~4 weekend gaps, below default min_samples=20
trade_client_few = MagicMock()
trade_client_few.get_stock_bars.return_value = {"SPY": few_bars}
gap_em_thin, n_thin = bot.estimate_weekend_gap_em(trade_client_few, "SPY", SPOT, gap_calendar_days=3)
assert gap_em_thin == 0.0 and n_thin < bot.GAP_MIN_SAMPLES
print(f"TEST 3 (thin sample n={n_thin} -> gap_em=0.0) PASSED\n")

# --- Test 4: disabled flag skips entirely, no API call made ---
old_flag = bot.GAP_BUFFER_ENABLED
bot.GAP_BUFFER_ENABLED = False
trade_client_disabled = MagicMock()
gap_em_disabled, n_disabled = bot.estimate_weekend_gap_em(trade_client_disabled, "SPY", SPOT, gap_calendar_days=3)
assert gap_em_disabled == 0.0 and n_disabled == 0
trade_client_disabled.get_stock_bars.assert_not_called()
bot.GAP_BUFFER_ENABLED = old_flag
print("TEST 4 (disabled flag -> no API call, gap_em=0.0) PASSED\n")

# --- Test 5: feed fallback (sip rejected -> iex succeeds) ---
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed

old_feed = bot.STOCK_DATA_FEED
bot.STOCK_DATA_FEED = DataFeed.SIP
trade_client_fallback = MagicMock()
trade_client_fallback.get_stock_bars.side_effect = [
    APIError("subscription does not permit querying recent SIP data"),
    {"SPY": bars},
]
gap_em_fb, n_fb = bot.estimate_weekend_gap_em(trade_client_fallback, "SPY", SPOT, gap_calendar_days=3)
assert n_fb >= 100 and gap_em_fb > 0
assert trade_client_fallback.get_stock_bars.call_count == 2
bot.STOCK_DATA_FEED = old_feed
print("TEST 5 (sip rejected -> falls back to iex) PASSED\n")

print("ALL WEEKEND GAP EM TESTS PASSED")
