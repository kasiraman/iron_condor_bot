"""
Approximate backtest of the SPY 0DTE iron condor strategy used in iron_condor_bot.py.

Data source: real SPY daily OHLC pulled from Alpha Vantage (free tier -> capped at the
most recent ~100 trading days: 2026-02-17 through 2026-07-10). This is shorter than the
6 months requested; the free API tier does not allow further back-history or premium
endpoints (VIX / real option chains / real IV are premium-gated on this key).

Because real historical option quotes/IV are not available on this key, this backtest
approximates the strategy's IV input with trailing REALIZED volatility computed from
SPY's own daily returns (20-day rolling window, annualized). This is a meaningfully
different number than implied volatility:
  - Real IV usually runs richer than realized vol (the "volatility risk premium"),
    especially for SPX/SPY. That means this backtest likely UNDERSTATES both the
    Expected Move (so strikes would sit a bit closer to the real EM-based ones) and
    the credit collected (theoretical B-S prices computed here are probably a bit
    lower than what the market would actually pay for the same strikes).
  - There's no bid/ask spread, slippage, or commissions modeled — credit received is
    the theoretical Black-Scholes mid price for each leg.
  - Assumes a 0DTE contract exists every single trading day. If SPY doesn't actually
    list expirations on such day (e.g., only M/W/F expirations were listed), real
    trade frequency would be lower than what's simulated here.

Treat this as a directional sanity check on the strategy's logic, not a substitute for
a real vendor-data backtest.
"""

import csv
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from scipy.stats import norm

TIMEZONE = ZoneInfo("America/New_York")
BASE = Path(__file__).parent

EM_MULTIPLIER = 1.25
WING_FRACTION = 0.5
QTY = 1
RISK_FREE_RATE = 0.05
MAX_RISK_PER_TRADE_USD = 500.0
VOL_LOOKBACK_DAYS = 20
STRIKE_INCREMENT = 1.0  # SPY strikes rounded to nearest $1


def bs_price(S, K, T, r, sigma, option_type):
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if option_type == "call":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def year_fraction_to_close(entry_hour=9, entry_minute=31):
    """Same convention as the live bot: fraction of a year remaining from ~9:31am to 4:00pm ET."""
    dummy_date = datetime(2000, 1, 1, entry_hour, entry_minute, tzinfo=TIMEZONE)
    close = dummy_date.replace(hour=16, minute=0, second=0, microsecond=0)
    seconds_remaining = (close - dummy_date).total_seconds()
    return (seconds_remaining / 86400.0) / 365.0


def round_to_increment(value, increment):
    return round(value / increment) * increment


def load_spy_series():
    with open(BASE / "spy_daily_raw.json") as f:
        raw = json.load(f)
    series = raw["Time Series (Daily)"]
    rows = []
    for date_str, vals in series.items():
        rows.append({
            "date": date_str,
            "open": float(vals["1. open"]),
            "high": float(vals["2. high"]),
            "low": float(vals["3. low"]),
            "close": float(vals["4. close"]),
        })
    rows.sort(key=lambda r: r["date"])
    return rows


def realized_vol(closes, lookback):
    """Annualized realized vol from trailing daily log returns, using `lookback` prior
    closes (not including the current day) — so no lookahead bias."""
    if len(closes) < lookback + 1:
        return None
    window = closes[-(lookback + 1):]
    log_returns = [math.log(window[i] / window[i - 1]) for i in range(1, len(window))]
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    daily_std = math.sqrt(variance)
    return daily_std * math.sqrt(252)


def run_backtest():
    rows = load_spy_series()
    T = year_fraction_to_close()

    results = []
    skipped = []

    for i in range(len(rows)):
        if i < VOL_LOOKBACK_DAYS:
            continue  # not enough history yet to compute a trailing realized vol

        prior_closes = [r["close"] for r in rows[max(0, i - VOL_LOOKBACK_DAYS - 1):i]]
        iv = realized_vol(prior_closes, VOL_LOOKBACK_DAYS)
        if iv is None or iv <= 0:
            continue

        day = rows[i]
        spot = day["open"]
        close = day["close"]

        em = spot * iv * math.sqrt(T)

        short_put_t = spot - EM_MULTIPLIER * em
        short_call_t = spot + EM_MULTIPLIER * em
        long_put_t = short_put_t - WING_FRACTION * em
        long_call_t = short_call_t + WING_FRACTION * em

        short_put = round_to_increment(short_put_t, STRIKE_INCREMENT)
        short_call = round_to_increment(short_call_t, STRIKE_INCREMENT)
        long_put = round_to_increment(long_put_t, STRIKE_INCREMENT)
        long_call = round_to_increment(long_call_t, STRIKE_INCREMENT)

        sp_price = bs_price(spot, short_put, T, RISK_FREE_RATE, iv, "put")
        lp_price = bs_price(spot, long_put, T, RISK_FREE_RATE, iv, "put")
        sc_price = bs_price(spot, short_call, T, RISK_FREE_RATE, iv, "call")
        lc_price = bs_price(spot, long_call, T, RISK_FREE_RATE, iv, "call")

        net_credit = (sp_price + sc_price) - (lp_price + lc_price)

        put_wing = short_put - long_put
        call_wing = long_call - short_call
        max_wing = max(put_wing, call_wing)
        max_risk = (max_wing - net_credit) * 100 * QTY

        reason = None
        if net_credit <= 0:
            reason = "non-positive credit"
        elif max_risk > MAX_RISK_PER_TRADE_USD:
            reason = f"max risk ${max_risk:.2f} > cap ${MAX_RISK_PER_TRADE_USD:.0f}"

        if reason:
            skipped.append({"date": day["date"], "reason": reason})
            continue

        settlement_value = (
            max(0.0, short_put - close) - max(0.0, long_put - close)
            + max(0.0, close - short_call) - max(0.0, close - long_call)
        )
        pnl = (net_credit - settlement_value) * 100 * QTY

        results.append({
            "date": day["date"],
            "spot_open": spot,
            "close": close,
            "iv_realized": iv,
            "em": em,
            "short_put": short_put,
            "long_put": long_put,
            "short_call": short_call,
            "long_call": long_call,
            "net_credit": net_credit,
            "max_risk": max_risk,
            "settlement_value": settlement_value,
            "pnl": pnl,
        })

    return results, skipped


def summarize(results):
    n = len(results)
    if n == 0:
        print("No trades simulated.")
        return

    pnls = [r["pnl"] for r in results]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total_pnl = sum(pnls)
    win_rate = len(wins) / n
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    avg_credit = sum(r["net_credit"] for r in results) / n * 100
    avg_em_pct = sum(r["em"] / r["spot_open"] for r in results) / n

    # equity curve + max drawdown
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    print(f"Trades simulated:      {n}  ({results[0]['date']} to {results[-1]['date']})")
    print(f"Total P&L:             ${total_pnl:,.2f}")
    print(f"Win rate:              {win_rate:.1%}  ({len(wins)}W / {len(losses)}L)")
    print(f"Avg credit collected:  ${avg_credit:,.2f} /contract")
    print(f"Avg win:               ${avg_win:,.2f}")
    print(f"Avg loss:              ${avg_loss:,.2f}")
    print(f"Avg EM (% of spot):    {avg_em_pct:.2%}")
    print(f"Max drawdown:          ${max_dd:,.2f}")
    print(f"Final cumulative P&L:  ${equity:,.2f}")


def main():
    results, skipped = run_backtest()

    out_csv = BASE / "backtest_results.csv"
    with open(out_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)

    print(f"Wrote {len(results)} trade rows to {out_csv.name}")
    if skipped:
        print(f"Skipped {len(skipped)} day(s) due to risk filters:")
        for s in skipped:
            print(f"  {s['date']}: {s['reason']}")
    print()
    summarize(results)


if __name__ == "__main__":
    main()
