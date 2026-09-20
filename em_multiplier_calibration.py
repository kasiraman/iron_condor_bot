"""
Data-driven calibration of the 0DTE bot's EM_MULTIPLIER -- replaces manually toggling
between values like 1.25 and 1.5 by hand with a backtest-derived number. See the README's
"Data-driven EM multiplier" section for the full write-up; read that before trusting this.

THE CORE APPROXIMATION (read this before trusting the numbers):
We don't have historical per-day option-implied IV -- reconstructing it would mean pulling
years of historical options quotes, too heavy a lift for this project. Instead, for each
historical day we proxy "what that morning's IV-implied EM would have been" using a
TRAILING REALIZED volatility of SPY's own recent intraday (open-to-close) moves, computed
strictly from days BEFORE that day (no lookahead). Realized vol usually runs a bit below
option-implied vol (the market's vol risk premium), so calibrating against it and then
applying the result to today's live IV-based EM carries a mild built-in safety cushion
rather than a shortfall -- but it's still an approximation of the real quantity you care
about (how live EM relates to the realized move), not a direct backtest of it.

METHOD:
  1. For each historical day i (with `vol_window` prior days of data available):
       trailing_vol_i = stdev of the PRIOR `vol_window` days' own intraday returns
                        ((close - open) / open), using only data strictly before day i.
       predicted_move_i = open_i * trailing_vol_i
       realized_move_i  = abs(close_i - open_i)
       ratio_i = realized_move_i / predicted_move_i
     ratio_i is dimensionless: "how many trailing-vol-implied sigmas was day i's actual
     move." No separate annualization/day-count conversion is needed here, since both
     predicted_move_i and realized_move_i already describe the same one-session timescale.
  2. Days are split into an EVENT bucket (FOMC/CPI/NFP/opex, via event_calendar.py) and an
     ORDINARY bucket. This is what lets the calibrated multiplier come out wider on event
     days without hand-tuning it that way.
  3. The calibrated multiplier for a given bucket is the target percentile of that
     bucket's ratio distribution (e.g. the 95th percentile means only ~5% of that bucket's
     historical days would have closed outside spot +/- multiplier*EM).
  4. If the bucket that applies to TODAY has fewer than `min_samples` historical days,
     falls back to the pooled (event + ordinary) distribution, with the shortfall noted in
     the returned detail string. If even the pooled sample is too small, returns None and
     the caller should fall back to a fixed manual multiplier.
"""
import math
import statistics
from datetime import timedelta

from alpaca.common.exceptions import APIError
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

from event_calendar import is_event_day


def _fetch_daily_bars_range(stock_data_client, symbol, start, end, feed):
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=end, feed=feed)
    bars = stock_data_client.get_stock_bars(req)
    return bars[symbol]


def _percentile(data, pct):
    """Linear-interpolation percentile (same convention as numpy's default), 0 <= pct <= 100."""
    if not data:
        return None
    data = sorted(data)
    k = (len(data) - 1) * (pct / 100.0)
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return data[int(k)]
    return data[f] + (data[c] - data[f]) * (k - f)


def calibrate_em_multiplier(
    stock_data_client, symbol, today,
    lookback_days=730, vol_window=20, target_percentile=95.0, min_samples=20,
    feed=DataFeed.IEX,
):
    """Returns (multiplier, n_samples_used, is_today_event_day, detail):
      multiplier         -- the calibrated value to use today, or None if there wasn't
                             enough historical data to compute anything trustworthy
                             (caller should fall back to a fixed EM_MULTIPLIER).
      n_samples_used      -- how many historical days went into whichever bucket was
                             actually used.
      is_today_event_day  -- whether `today` is a known FOMC/CPI/NFP/opex day.
      detail              -- "event" / "ordinary" if that bucket's own sample was used
                             directly, "pooled (...)" if it fell back to the combined
                             event+ordinary sample, or a reason string if multiplier is None.
    """
    today_is_event = is_event_day(today)

    end = today
    start = end - timedelta(days=lookback_days + vol_window + 10)  # pad for the vol warm-up window

    try:
        bars = _fetch_daily_bars_range(stock_data_client, symbol, start, end, feed)
    except APIError as e:
        if feed == DataFeed.IEX:
            return None, 0, today_is_event, f"could not fetch historical bars ({e})"
        try:
            bars = _fetch_daily_bars_range(stock_data_client, symbol, start, end, DataFeed.IEX)
        except Exception as e2:
            return None, 0, today_is_event, f"could not fetch historical bars ({e2})"

    # Exclude today's own bar (if present at all, it'd be incomplete intraday) and
    # anything else not strictly before today -- we're backtesting, not looking ahead.
    bars = [b for b in bars if b.timestamp.date() < today]

    if len(bars) < vol_window + min_samples:
        return None, len(bars), today_is_event, f"only {len(bars)} historical bar(s) available (need >= {vol_window + min_samples})"

    intraday_returns = [(float(b.close) - float(b.open)) / float(b.open) for b in bars]

    event_ratios, ordinary_ratios = [], []
    for i in range(vol_window, len(bars)):
        trailing = intraday_returns[i - vol_window:i]
        try:
            trailing_vol = statistics.stdev(trailing)
        except statistics.StatisticsError:
            continue
        if trailing_vol <= 0:
            continue
        predicted_move = float(bars[i].open) * trailing_vol
        realized_move = abs(float(bars[i].close) - float(bars[i].open))
        ratio = realized_move / predicted_move
        bucket = event_ratios if is_event_day(bars[i].timestamp.date()) else ordinary_ratios
        bucket.append(ratio)

    own_bucket = event_ratios if today_is_event else ordinary_ratios
    own_label = "event" if today_is_event else "ordinary"

    if len(own_bucket) >= min_samples:
        return _percentile(own_bucket, target_percentile), len(own_bucket), today_is_event, own_label

    pooled = event_ratios + ordinary_ratios
    if len(pooled) >= min_samples:
        return (
            _percentile(pooled, target_percentile), len(pooled), today_is_event,
            f"pooled (only {len(own_bucket)} {own_label}-day sample(s), need >= {min_samples})",
        )

    return None, len(pooled), today_is_event, f"only {len(pooled)} usable historical ratio(s) in any bucket (need >= {min_samples})"
