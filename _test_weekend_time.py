"""Ad-hoc verification of weekend_time.py's dual T convention -- not part of the
deployed bot, just a one-off check run during development. Uses the real
alpaca.trading.models.Calendar class (constructed the same way alpaca-py parses real API
responses) so this exercises the exact same open/close datetime shape get_trading_calendar()
would hand back for real, without needing a live Alpaca connection."""

from datetime import datetime
from zoneinfo import ZoneInfo

from alpaca.trading.models import Calendar

from weekend_time import (
    TIMEZONE,
    session_open_dt,
    session_close_dt,
    year_fraction_to_close_calendar,
    year_fraction_to_close_trading_hours,
)

# Friday 2026-08-14, Saturday/Sunday off, Monday 2026-08-17 (matches this week's actual calendar)
fri = Calendar(date="2026-08-14", open="09:30", close="16:00")
mon = Calendar(date="2026-08-17", open="09:30", close="16:00")

now = datetime(2026, 8, 14, 9, 31, tzinfo=TIMEZONE)  # Friday 9:31am ET entry
close_dt = session_close_dt(mon)  # Monday 4:00pm ET expiration

# --- calendar T: real elapsed time, weekend included ---
T_cal = year_fraction_to_close_calendar(now, close_dt)
expected_cal_days = (close_dt - now).total_seconds() / 86400.0
print(f"T_calendar = {T_cal:.6f}  (~{T_cal*365:.2f} calendar days)")
assert abs(T_cal * 365 - expected_cal_days) < 1e-9
# Friday 9:31am -> Monday 9:31am is exactly 3 days; Monday 9:31am -> Monday 4:00pm adds
# another ~6.4833h -- so the real total is ~3.27 days, not a flat 3.
assert 3.25 < T_cal * 365 < 3.29, "expected ~3.27 calendar days Friday 9:31am -> Monday 4pm"

# --- trading-hours T: only real session time, weekend excluded ---
calendar = [fri, mon]
T_th = year_fraction_to_close_trading_hours(now, close_dt, calendar)
th_hours = T_th * 365 * 24
print(f"T_trading_hours = {T_th:.6f}  (~{th_hours:.2f} trading hours)")
# Friday: 9:31am -> 4:00pm = 6h29m = 6.4833h ; Monday: 9:30am -> 4:00pm = 6.5h
expected_hours = 6.4833333 + 6.5
assert abs(th_hours - expected_hours) < 0.01, f"expected ~{expected_hours:.4f}h, got {th_hours:.4f}h"

# Sanity: trading-hours T should be dramatically smaller than calendar T (the whole point)
assert T_th < T_cal / 4, "trading-hours T should be much smaller than calendar T"
print(f"T_th / T_cal = {T_th / T_cal:.4f}  (trading-hours T is {T_cal / T_th:.1f}x smaller)")

# --- EM comparison at a fixed IV, to show the intended effect end to end ---
import math
spot = 645.0
iv = 0.12
em_cal = spot * iv * math.sqrt(T_cal)
em_th = spot * iv * math.sqrt(T_th)
print(f"EM using calendar T:       {em_cal:.2f} ({em_cal/spot:.2%} of spot)")
print(f"EM using trading-hours T:  {em_th:.2f} ({em_th/spot:.2%} of spot)")
assert em_th < em_cal, "trading-hours EM should size tighter strikes than calendar EM"

# --- holiday handling: Monday is a holiday, real next session is Tuesday ---
tue = Calendar(date="2026-08-18", open="09:30", close="16:00")
close_dt_holiday = session_close_dt(tue)
calendar_holiday = [fri, tue]  # note: Monday absent entirely, as get_trading_calendar() would return for a holiday
T_th_holiday = year_fraction_to_close_trading_hours(now, close_dt_holiday, calendar_holiday)
th_hours_holiday = T_th_holiday * 365 * 24
expected_hours_holiday = 6.4833333 + 6.5  # Friday remainder + full Tuesday session, Monday holiday contributes 0
print(f"T_trading_hours (Monday holiday, expires Tuesday) = {T_th_holiday:.6f}  (~{th_hours_holiday:.2f} trading hours)")
assert abs(th_hours_holiday - expected_hours_holiday) < 0.01

# --- edge case: now already past close_dt should floor to a tiny positive T, not blow up ---
past = datetime(2026, 8, 17, 16, 30, tzinfo=TIMEZONE)
T_cal_past = year_fraction_to_close_calendar(past, close_dt)
T_th_past = year_fraction_to_close_trading_hours(past, close_dt, calendar)
assert T_cal_past > 0 and T_th_past > 0
print(f"Past-close floor: T_calendar={T_cal_past:.9f}  T_trading_hours={T_th_past:.9f}")

print("\nALL WEEKEND_TIME CHECKS PASSED")
