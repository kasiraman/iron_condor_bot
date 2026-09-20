"""
SPY 0DTE Iron Condor bot (Alpaca paper/live trading).

NOTE: this is the renamed 0dte_ version of what used to be iron_condor_bot.py -- same
code, same logs/trades.csv (unprefixed, kept for continuity with existing history), just
a clearer name now that a second strategy (weekend_iron_condor_bot.py, which holds a
Friday entry through to Monday's expiration) exists alongside it.

Strategy:
  1. Pull SPY spot price and today's (0DTE) option chain.
  2. Solve implied volatility off the ATM straddle (Black-Scholes / Brent's method).
  3. Expected Move:  EM = spot * IV * sqrt(DTE_fraction / 365)
     where DTE_fraction is the remaining fraction of the trading day (time now -> 4:00pm ET),
     expressed as a day-count, consistent with the IV solve's own T.
  4. Short strikes  = spot +/- (EM_MULTIPLIER * EM)      [default 1.25x, or a backtest-
     calibrated value when EM_MULTIPLIER_MODE=calibrated -- see em_multiplier_calibration.py
     and the README's "Data-driven EM multiplier" section]
  5. Long strikes   = short strike +/- (WING_FRACTION * EM)  [default 0.5x, i.e. wing width scales with EM]
  6. Submit a single 4-leg MLEG limit order (sell iron condor) on Alpaca.

IMPORTANT -- read before relying on this:
  - Alpaca does not yet support SPX/XSP index options (confirmed via their docs as of this writing).
    This bot trades SPY (an ETF proxy for SPX, ~1/10th price, physically settled) instead.
  - This script places ENTRY orders only. Exit management (profit target, stop loss) is handled
    by the companion 0dte_monitor_and_exit.py script.
  - Test extensively with --dry-run, then with real paper-account orders, before trusting it
    to run unattended.
  - Defaults to paper trading -- see ALPACA_PAPER in .env before ever pointing this at a live account.
"""

import argparse
import csv
import math
import os
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from dotenv import load_dotenv
from scipy.optimize import brentq
from scipy.stats import norm

from bot_logging import get_logger
from alpaca_config import ALPACA_PAPER, API_KEY, SECRET_KEY
from em_multiplier_calibration import calibrate_em_multiplier
from event_calendar import is_event_day, event_labels

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    GetOptionContractsRequest,
    OptionLegRequest,
    LimitOrderRequest,
)
from alpaca.trading.enums import (
    AssetStatus,
    ContractType,
    OrderClass,
    OrderSide,
    TimeInForce,
)
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient, StockLatestTradeRequest
from alpaca.data.requests import OptionLatestQuoteRequest
from alpaca.data.enums import DataFeed, OptionsFeed

# --------------------------------------------------------------------------
# Config -- tune these, or override via .env / environment variables
# --------------------------------------------------------------------------
load_dotenv()

# API_KEY / SECRET_KEY / ALPACA_PAPER come from alpaca_config.py (shared with the other
# scripts) -- see ALPACA_PAPER in .env to control paper vs. live trading.

UNDERLYING = os.getenv("UNDERLYING", "SPY")
EM_MULTIPLIER = float(os.getenv("EM_MULTIPLIER", "1.25"))      # short strike = spot +/- EM_MULTIPLIER * EM
WING_FRACTION = float(os.getenv("WING_FRACTION", "0.5"))       # long strike = short +/- WING_FRACTION * EM

# Data-driven alternative to manually toggling EM_MULTIPLIER by hand -- see
# em_multiplier_calibration.py and the README's "Data-driven EM multiplier" section
# before trusting this. Defaults OFF ("fixed", using EM_MULTIPLIER above unchanged) since
# this changes live strike placement and hasn't been observed on a real account yet --
# test with --dry-run (and ideally a stretch of paper trading) before setting
# EM_MULTIPLIER_MODE=calibrated in .env. Once enabled, it computes EM_MULTIPLIER from a
# historical backtest each run, falling back to the fixed EM_MULTIPLIER (with a warning)
# whenever there isn't enough historical data to trust.
EM_MULTIPLIER_MODE = os.getenv("EM_MULTIPLIER_MODE", "fixed").lower()
EM_MULTIPLIER_TARGET_PERCENTILE = float(os.getenv("EM_MULTIPLIER_TARGET_PERCENTILE", "95"))
EM_MULTIPLIER_LOOKBACK_DAYS = int(os.getenv("EM_MULTIPLIER_LOOKBACK_DAYS", "730"))
EM_MULTIPLIER_VOL_WINDOW = int(os.getenv("EM_MULTIPLIER_VOL_WINDOW", "20"))
EM_MULTIPLIER_MIN_SAMPLES = int(os.getenv("EM_MULTIPLIER_MIN_SAMPLES", "20"))
if EM_MULTIPLIER_MODE not in ("fixed", "calibrated"):
    raise RuntimeError(f"EM_MULTIPLIER_MODE must be 'fixed' or 'calibrated', got {EM_MULTIPLIER_MODE!r}.")
QTY = int(os.getenv("QTY", "1"))  # ceiling on contracts/leg -- actual qty is sized down to fit the risk budget below
RISK_FREE_RATE = float(os.getenv("RISK_FREE_RATE", "0.05"))
STRIKE_RANGE_PCT = float(os.getenv("STRIKE_RANGE_PCT", "0.08"))  # how wide a strike window to pull from the chain
CREDIT_BUFFER = float(os.getenv("CREDIT_BUFFER", "0.05"))        # shave this off mid-credit to help the limit order fill

# Per-trade risk budget: dollars, percent of account equity, or both. qty is sized to
# floor(risk_budget / risk_per_contract), capped at QTY. If both are set, the dollar
# amount acts as a hard ceiling on whatever the percentage of equity would otherwise
# allow -- e.g. "risk 2% of the account, but never more than $1,000 even as equity grows".
# If neither is set, falls back to a flat $500 (the original default).
_max_risk_usd_env = os.getenv("MAX_RISK_PER_TRADE_USD")
_max_risk_pct_env = os.getenv("MAX_RISK_PER_TRADE_PCT")
MAX_RISK_PER_TRADE_USD = float(_max_risk_usd_env) if _max_risk_usd_env else None
MAX_RISK_PER_TRADE_PCT = float(_max_risk_pct_env) if _max_risk_pct_env else None  # fraction, e.g. 0.02 = 2%
if MAX_RISK_PER_TRADE_USD is None and MAX_RISK_PER_TRADE_PCT is None:
    MAX_RISK_PER_TRADE_USD = 500.0

# INFORMATIONAL ONLY -- read here purely to log how much smaller a cleanly-firing stop
# loss would be versus the max-theoretical-loss figure qty is actually sized against
# (see the "Net credit" log line in build_iron_condor()). This does NOT change sizing --
# qty is still floor(risk_budget / max_loss_per_contract), full stop. The real,
# behavior-controlling copy of this value lives in 0dte_monitor_and_exit.py; it's read
# again here (same env var, same default) only so this log line reflects whatever you've
# actually configured the monitor's stop-loss to be. Max-loss sizing is intentionally
# more conservative than a stop-loss-based figure would be, since the stop can't be
# trusted to fire cleanly in every scenario (monitor downtime, the post-MONITOR_END_TIME
# blind window, slow/illiquid fills during a fast move) -- see the README's "Contract
# sizing vs. stop-loss" section for the full discussion.
STOP_LOSS_PCT_INFO = float(os.getenv("STOP_LOSS_PCT", "1.20"))

LOG_DIR = Path(__file__).parent / "logs"
TRADE_LOG_CSV = LOG_DIR / "trades.csv"  # unprefixed on purpose -- preserves existing trade history
EM_MULTIPLIER_LOG_CSV = LOG_DIR / "em_multiplier_log.csv"

# Free/basic Alpaca accounts only get the IEX stock feed and "indicative" (not real-time
# OPRA) option data -- the SDK's defaults require a paid subscription and raise
# "subscription does not permit querying recent SIP data" otherwise. Override via .env
# (STOCK_DATA_FEED=sip / OPTION_DATA_FEED=opra) if you do have a paid plan.
STOCK_DATA_FEED = DataFeed(os.getenv("STOCK_DATA_FEED", "iex"))
OPTION_DATA_FEED = OptionsFeed(os.getenv("OPTION_DATA_FEED", "indicative"))

TIMEZONE = ZoneInfo("America/New_York")

log = get_logger("0dte_iron_condor_bot")


# --------------------------------------------------------------------------
# Black-Scholes helpers (used to back out IV from the ATM straddle mid-price)
# --------------------------------------------------------------------------
def bs_price(S, K, T, r, sigma, option_type):
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if option_type == "call":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def implied_volatility(price, S, K, T, r, option_type):
    intrinsic = max(0.0, (S - K) if option_type == "call" else (K - S))
    if price <= intrinsic + 1e-6:
        return None
    try:
        return brentq(lambda sigma: bs_price(S, K, T, r, sigma, option_type) - price, 1e-6, 5.0)
    except ValueError:
        return None


def year_fraction_to_close(now: datetime) -> float:
    """Fraction of a year remaining until today's 4:00pm ET close (0DTE T)."""
    close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    seconds_remaining = max((close - now).total_seconds(), 1.0)
    return (seconds_remaining / 86400.0) / 365.0


# --------------------------------------------------------------------------
# Alpaca client setup
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
    stock_data_client = StockHistoricalDataClient(api_key=API_KEY, secret_key=SECRET_KEY)
    return trade_client, option_data_client, stock_data_client


def market_is_open_today(trade_client) -> bool:
    clock = trade_client.get_clock()
    return clock.is_open


def compute_risk_budget(trade_client) -> float:
    """Resolves the per-trade dollar risk budget from MAX_RISK_PER_TRADE_USD /
    MAX_RISK_PER_TRADE_PCT. Only calls Alpaca for account equity if a percent-based
    budget is actually configured -- no extra API call otherwise. If both a dollar
    and a percent budget are configured, returns whichever is LOWER, so the dollar
    figure acts as a hard ceiling on the percent-of-equity figure."""
    pct_budget = None
    if MAX_RISK_PER_TRADE_PCT is not None:
        account = trade_client.get_account()
        equity = float(account.equity)
        pct_budget = equity * MAX_RISK_PER_TRADE_PCT
        log.info(f"Account equity: ${equity:,.2f} -> MAX_RISK_PER_TRADE_PCT ({MAX_RISK_PER_TRADE_PCT:.2%}) budget = ${pct_budget:,.2f}")

    if MAX_RISK_PER_TRADE_USD is not None and pct_budget is not None:
        budget = min(MAX_RISK_PER_TRADE_USD, pct_budget)
        log.info(
            f"Both a dollar (${MAX_RISK_PER_TRADE_USD:,.2f}) and percent-of-equity (${pct_budget:,.2f}) "
            f"risk budget are configured -- using the lower of the two: ${budget:,.2f}"
        )
        return budget
    if MAX_RISK_PER_TRADE_USD is not None:
        return MAX_RISK_PER_TRADE_USD
    return pct_budget


def get_spot_price(stock_data_client, symbol):
    req = StockLatestTradeRequest(symbol_or_symbols=symbol, feed=STOCK_DATA_FEED)
    resp = stock_data_client.get_stock_latest_trade(req)
    return float(resp[symbol].price)


def get_today_chain(trade_client, symbol, spot, target_date=None):
    """Fetches the option chain for the nearest available expiration on/after `target_date`
    (defaults to today). On a real trading day during market hours this resolves to an
    actual same-day (0DTE) expiration. On a weekend/holiday, or when testing with --force
    outside of a real trading day, there simply is no expiration dated `target_date` --
    rather than erroring out, this falls forward to whatever the nearest listed expiration
    actually is, so --dry-run/--force testing works any day of the week. It logs clearly
    when that happens so it's never mistaken for a real 0DTE trade."""
    today = target_date or datetime.now(TIMEZONE).date()
    window_end = today + timedelta(days=10)  # wide enough to bridge any weekend/holiday gap
    min_strike = spot * (1 - STRIKE_RANGE_PCT)
    max_strike = spot * (1 + STRIKE_RANGE_PCT)

    calls_req = GetOptionContractsRequest(
        underlying_symbols=[symbol],
        status=AssetStatus.ACTIVE,
        type=ContractType.CALL,
        strike_price_gte=str(min_strike),
        strike_price_lte=str(max_strike),
        expiration_date_gte=today,
        expiration_date_lte=window_end,
    )
    puts_req = GetOptionContractsRequest(
        underlying_symbols=[symbol],
        status=AssetStatus.ACTIVE,
        type=ContractType.PUT,
        strike_price_gte=str(min_strike),
        strike_price_lte=str(max_strike),
        expiration_date_gte=today,
        expiration_date_lte=window_end,
    )
    all_calls = trade_client.get_option_contracts(calls_req).option_contracts
    all_puts = trade_client.get_option_contracts(puts_req).option_contracts

    if not all_calls or not all_puts:
        raise RuntimeError(
            f"No {symbol} option contracts found expiring between {today} and {window_end} "
            f"within +/-{STRIKE_RANGE_PCT:.0%} of spot. Widen STRIKE_RANGE_PCT or confirm the "
            "underlying/strike range are correct."
        )

    call_expirations = {c.expiration_date for c in all_calls}
    put_expirations = {p.expiration_date for p in all_puts}
    common_expirations = call_expirations & put_expirations
    if not common_expirations:
        raise RuntimeError(f"No common call/put expiration found for {symbol} in the fetched window.")

    nearest_expiration = min(common_expirations)
    if nearest_expiration != today:
        log.warning(
            f"No expiration dated {today} (weekend/holiday, or after today's expirations were listed) -- "
            f"using nearest available expiration {nearest_expiration} instead. This is NOT a same-day "
            "0DTE trade; treat results as a structural/logic test only."
        )

    calls = [c for c in all_calls if c.expiration_date == nearest_expiration]
    puts = [p for p in all_puts if p.expiration_date == nearest_expiration]
    return calls, puts


def get_quotes(option_data_client, symbols):
    req = OptionLatestQuoteRequest(symbol_or_symbols=symbols, feed=OPTION_DATA_FEED)
    quotes = option_data_client.get_option_latest_quote(req)
    return quotes


def mid(quote):
    return (float(quote.bid_price) + float(quote.ask_price)) / 2.0


def nearest_contract(contracts, target_strike):
    return min(contracts, key=lambda c: abs(float(c.strike_price) - target_strike))


# --------------------------------------------------------------------------
# Core strategy logic
# --------------------------------------------------------------------------
def build_iron_condor(trade_client, option_data_client, stock_data_client, target_date=None, test_iv=None):
    """If `test_iv` is provided, the live ATM-straddle IV solve AND the live bid/ask credit
    calc are both skipped, in favor of a fixed IV and Black-Scholes theoretical leg prices.
    This is for structural/logic testing when the market is closed (or quotes are otherwise
    stale/empty, e.g. testing against tomorrow's just-listed contracts over a weekend) --
    real trading days should NOT need --test-iv, since real quotes will be live.

    Returns None (after logging why) if today's risk budget can't afford even 1 contract at
    the computed strikes/credit -- this is an expected, non-error outcome (e.g. a low-IV day
    producing a tight condor whose risk/contract happens to exceed budget), not a bug, so it
    does not raise. A non-positive net credit or other structural problem with quotes still
    raises RuntimeError, since that indicates something is actually wrong with the pipeline."""
    now = datetime.now(TIMEZONE)
    spot = get_spot_price(stock_data_client, UNDERLYING)
    log.info(f"{UNDERLYING} spot: {spot:.2f}")

    calls, puts = get_today_chain(trade_client, UNDERLYING, spot, target_date=target_date)

    market_open_dt = now.replace(hour=9, minute=31, second=0, microsecond=0)
    market_close_dt = now.replace(hour=16, minute=0, second=0, microsecond=0)
    outside_hours = now < market_open_dt or now >= market_close_dt

    if outside_hours or test_iv is not None:
        # Simulate a normal 9:31 AM ET entry instead of using the real wall-clock time, in
        # two situations:
        #   1. Outside real market hours (weekend, holiday, or just running this at night) --
        #      the real time-to-close is meaningless there (negative/near-zero after the close,
        #      which used to collapse T to a 1-second floor and blow up the IV solve into
        #      nonsense like 496% IV).
        #   2. --test-iv is set -- this is explicitly a structural/logic test, and should give
        #      the same, consistent "as if this were market open" result no matter what time of
        #      day you actually run it. Without this, running --test-iv mid-afternoon would still
        #      use the real (small) remaining time and produce a misleadingly thin/degenerate
        #      condor that has nothing to do with what a real 9:31 AM entry would look like.
        # NOTE: this does NOT apply to a real run during real market hours without --test-iv --
        # that intentionally reflects the true remaining time-to-close, since EM (and therefore
        # the strikes) legitimately shrinks as the day goes on. This bot is designed to enter
        # once near 9:31 AM ET; running it for real later in the day and getting a thinner/no
        # credit is expected behavior, not a bug -- see the "Entry timing" note in the README.
        reference_for_T = market_open_dt
        if outside_hours:
            log.warning(
                f"Current time ({now.strftime('%H:%M %Z')}) is outside real market hours (9:30-16:00 ET) -- "
                "simulating time-to-close as if it were 9:31 AM ET for T/IV/EM purposes."
            )
        else:
            log.warning(
                "--test-iv set: simulating time-to-close as if it were 9:31 AM ET (not the actual "
                "current time), so structural testing is consistent regardless of when you run it."
            )
    else:
        reference_for_T = now

    T = year_fraction_to_close(reference_for_T)

    if test_iv is not None:
        iv = test_iv
        log.warning(
            f"--test-iv {iv:.1%} set: skipping live ATM quotes/IV-solve entirely -- using this fixed IV instead. "
            "Simulated, not from live market data."
        )
    else:
        # ATM strike = closest available strike to spot (use whichever side has it; check both)
        atm_call = nearest_contract(calls, spot)
        atm_put = nearest_contract(puts, spot)

        quotes = get_quotes(option_data_client, [atm_call.symbol, atm_put.symbol])
        call_mid = mid(quotes[atm_call.symbol])
        put_mid = mid(quotes[atm_put.symbol])

        iv_call = implied_volatility(call_mid, spot, float(atm_call.strike_price), T, RISK_FREE_RATE, "call")
        iv_put = implied_volatility(put_mid, spot, float(atm_put.strike_price), T, RISK_FREE_RATE, "put")

        ivs = [iv for iv in (iv_call, iv_put) if iv is not None]
        if not ivs:
            raise RuntimeError(
                "Could not solve implied volatility from the ATM straddle -- the bid/ask quotes were "
                "probably empty/stale (e.g. market closed, or contracts just listed with no trades yet). "
                "Pass --test-iv 0.15 (or your own estimate) to bypass live quotes and test the rest of "
                "the pipeline structurally."
            )
        iv = float(np.mean(ivs))
        log.info(f"ATM straddle mid: call={call_mid:.2f} put={put_mid:.2f} -> IV={iv:.1%} (T={T:.6f}y)")

    em = spot * iv * math.sqrt(T)
    log.info(f"Expected Move (EM): {em:.2f}  ({em/spot:.2%} of spot)")

    effective_multiplier = EM_MULTIPLIER
    calibrated_multiplier = None
    n_samples = 0
    today_for_calibration = target_date or now.date()
    is_event = is_event_day(today_for_calibration)
    labels = event_labels(today_for_calibration) if is_event else []
    detail = "fixed (EM_MULTIPLIER_MODE=fixed)"

    if EM_MULTIPLIER_MODE == "calibrated":
        calibrated_multiplier, n_samples, is_event, detail = calibrate_em_multiplier(
            stock_data_client, UNDERLYING, today_for_calibration,
            lookback_days=EM_MULTIPLIER_LOOKBACK_DAYS,
            vol_window=EM_MULTIPLIER_VOL_WINDOW,
            target_percentile=EM_MULTIPLIER_TARGET_PERCENTILE,
            min_samples=EM_MULTIPLIER_MIN_SAMPLES,
            feed=STOCK_DATA_FEED,
        )
        labels = event_labels(today_for_calibration) if is_event else []
        if calibrated_multiplier is not None:
            effective_multiplier = calibrated_multiplier
            event_note = f"EVENT DAY ({', '.join(labels)})" if is_event else "ordinary day"
            log.info(
                f"EM multiplier: {effective_multiplier:.3f} [calibrated, {detail}, n={n_samples}, "
                f"{event_note}, target percentile={EM_MULTIPLIER_TARGET_PERCENTILE:.0f}] "
                f"(fixed fallback would be {EM_MULTIPLIER})"
            )
        else:
            log.warning(
                f"Could not compute a calibrated EM multiplier ({detail}) -- falling back to the "
                f"fixed EM_MULTIPLIER={EM_MULTIPLIER}."
            )
    else:
        log.info(f"EM multiplier: {effective_multiplier} [fixed -- set EM_MULTIPLIER_MODE=calibrated to use the backtest instead]")

    # Logged to its own CSV (not trades.csv -- that schema is fixed/append-only) so the
    # calibrated-vs-fixed decision can be reviewed later regardless of which mode was
    # actually used to trade that day, including days that were --dry-run, --test-iv, or
    # skipped entirely (e.g. risk budget too small) -- see log_multiplier_decision().
    log_multiplier_decision({
        "date": today_for_calibration.isoformat(),
        "timestamp": now.isoformat(),
        "mode": EM_MULTIPLIER_MODE,
        "test_iv_mode": test_iv is not None,
        "fixed_multiplier": EM_MULTIPLIER,
        "calibrated_multiplier": calibrated_multiplier,
        "effective_multiplier": effective_multiplier,
        "source_detail": detail,
        "n_samples": n_samples,
        "is_event_day": is_event,
        "event_labels": labels,
        "target_percentile": EM_MULTIPLIER_TARGET_PERCENTILE,
        "lookback_days": EM_MULTIPLIER_LOOKBACK_DAYS,
        "vol_window": EM_MULTIPLIER_VOL_WINDOW,
        "min_samples": EM_MULTIPLIER_MIN_SAMPLES,
    })

    short_put_target = spot - effective_multiplier * em
    short_call_target = spot + effective_multiplier * em
    long_put_target = short_put_target - WING_FRACTION * em
    long_call_target = short_call_target + WING_FRACTION * em

    short_put = nearest_contract(puts, short_put_target)
    long_put = nearest_contract(puts, long_put_target)
    short_call = nearest_contract(calls, short_call_target)
    long_call = nearest_contract(calls, long_call_target)

    log.info(
        "Target strikes -> short_put=%.2f long_put=%.2f short_call=%.2f long_call=%.2f",
        short_put_target, long_put_target, short_call_target, long_call_target,
    )
    log.info(
        "Selected contracts -> short_put=%s long_put=%s short_call=%s long_call=%s",
        short_put.symbol, long_put.symbol, short_call.symbol, long_call.symbol,
    )

    if test_iv is not None:
        sp_k, lp_k = float(short_put.strike_price), float(long_put.strike_price)
        sc_k, lc_k = float(short_call.strike_price), float(long_call.strike_price)
        net_credit = (
            bs_price(spot, sp_k, T, RISK_FREE_RATE, iv, "put")
            + bs_price(spot, sc_k, T, RISK_FREE_RATE, iv, "call")
            - bs_price(spot, lp_k, T, RISK_FREE_RATE, iv, "put")
            - bs_price(spot, lc_k, T, RISK_FREE_RATE, iv, "call")
        )
        log.warning(f"--test-iv set: net_credit is a Black-Scholes theoretical estimate, not from live quotes.")
    else:
        leg_symbols = [short_put.symbol, long_put.symbol, short_call.symbol, long_call.symbol]
        leg_quotes = get_quotes(option_data_client, leg_symbols)

        net_credit = (
            float(leg_quotes[short_put.symbol].bid_price)
            + float(leg_quotes[short_call.symbol].bid_price)
            - float(leg_quotes[long_put.symbol].ask_price)
            - float(leg_quotes[long_call.symbol].ask_price)
        )

    if net_credit <= 0:
        raise RuntimeError(f"Computed net credit is non-positive ({net_credit:.2f}) -- aborting, check quotes.")

    put_wing_width = float(short_put.strike_price) - float(long_put.strike_price)
    call_wing_width = float(long_call.strike_price) - float(short_call.strike_price)
    max_wing_width = max(put_wing_width, call_wing_width)
    risk_per_contract = (max_wing_width - net_credit) * 100

    if risk_per_contract <= 0:
        raise RuntimeError(
            f"Computed risk per contract is non-positive (${risk_per_contract:.2f}) -- net credit "
            f"(${net_credit:.2f}/contract) exceeds the max wing width (${max_wing_width:.2f}), which "
            "shouldn't happen for a real iron condor. Check quotes/strikes before trusting this."
        )

    # Size the position to use as much of the risk budget as the strikes/credit allow,
    # capped at QTY (now a ceiling, not a fixed size) -- rather than trading a fixed QTY
    # and simply refusing to trade at all whenever that fixed size happens to exceed budget.
    risk_budget = compute_risk_budget(trade_client)
    max_affordable_qty = math.floor(risk_budget / risk_per_contract)
    if max_affordable_qty < 1:
        log.warning(
            f"Even 1 contract's risk (${risk_per_contract:.2f}) exceeds the risk budget "
            f"(${risk_budget:.2f}) -- skipping entry today, no order submitted. This usually just means "
            "a low-IV day produced a tight condor whose risk/contract happens to exceed budget; if you "
            "want to trade through days like this, raise MAX_RISK_PER_TRADE_USD/MAX_RISK_PER_TRADE_PCT. "
            "(If this fires often, also check whether EM_MULTIPLIER/WING_FRACTION are producing wider "
            "wings than intended.)"
        )
        return None

    qty = min(QTY, max_affordable_qty)
    max_risk = risk_per_contract * qty

    # INFORMATIONAL ONLY (see STOP_LOSS_PCT_INFO above) -- shows how much smaller the
    # realized loss would be if the stop-loss fires cleanly at its configured threshold,
    # versus the max-theoretical-loss figure qty is actually sized against. Does not
    # affect qty/max_risk above in any way.
    stop_loss_risk_per_contract = STOP_LOSS_PCT_INFO * net_credit * 100
    stop_loss_implied_risk = stop_loss_risk_per_contract * qty
    utilization_pct = (stop_loss_risk_per_contract / risk_per_contract) if risk_per_contract else 0.0

    log.info(
        f"Net credit (mid): {net_credit:.2f}/contract | Risk/contract (max loss, used for sizing): ${risk_per_contract:.2f} | "
        f"Max affordable qty: {max_affordable_qty} (budget ${risk_budget:.2f}) | QTY cap: {QTY} | "
        f"Using qty={qty} | Max risk: ${max_risk:.2f}"
    )
    log.info(
        f"[sizing info] If the stop-loss fires cleanly at its configured {STOP_LOSS_PCT_INFO:.0%} threshold: "
        f"${stop_loss_risk_per_contract:.2f}/contract (${stop_loss_implied_risk:.2f} at qty={qty}) -- "
        f"{utilization_pct:.1%} of the max-loss figure this trade is sized against. The rest is safety "
        f"margin for scenarios where the stop can't fire cleanly (see README)."
    )

    return {
        "spot": spot,
        "iv": iv,
        "em": em,
        "short_put": short_put,
        "long_put": long_put,
        "short_call": short_call,
        "long_call": long_call,
        "net_credit": net_credit,
        "max_risk": max_risk,
        "risk_budget": risk_budget,
        "qty": qty,
    }


def log_multiplier_decision(decision):
    """Appends one row per build_iron_condor() invocation to logs/em_multiplier_log.csv --
    kept SEPARATE from trades.csv (whose schema is fixed/append-only) so this can be
    reviewed and extended freely. Written unconditionally (not just on days that actually
    trade), including --dry-run, --test-iv, and days skipped for insufficient risk budget,
    so you can see what the calibration would have said on every day you ran the bot, not
    only the days it actually traded. Join to trades.csv on `date` (0DTE only trades once/
    day, so date is a safe join key here) to compare the multiplier actually used against
    what was submitted."""
    LOG_DIR.mkdir(exist_ok=True)
    is_new = not EM_MULTIPLIER_LOG_CSV.exists()
    with open(EM_MULTIPLIER_LOG_CSV, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow([
                "date", "timestamp", "mode", "test_iv_mode",
                "fixed_multiplier", "calibrated_multiplier", "effective_multiplier",
                "source_detail", "n_samples", "is_event_day", "event_labels",
                "target_percentile", "lookback_days", "vol_window", "min_samples",
            ])
        writer.writerow([
            decision["date"], decision["timestamp"], decision["mode"], decision["test_iv_mode"],
            f"{decision['fixed_multiplier']:.4f}",
            f"{decision['calibrated_multiplier']:.4f}" if decision["calibrated_multiplier"] is not None else "",
            f"{decision['effective_multiplier']:.4f}",
            decision["source_detail"], decision["n_samples"], decision["is_event_day"],
            "|".join(decision["event_labels"]),
            decision["target_percentile"], decision["lookback_days"],
            decision["vol_window"], decision["min_samples"],
        ])


def submit_iron_condor(trade_client, plan):
    legs = [
        OptionLegRequest(symbol=plan["short_put"].symbol, side=OrderSide.SELL, ratio_qty=1),
        OptionLegRequest(symbol=plan["long_put"].symbol, side=OrderSide.BUY, ratio_qty=1),
        OptionLegRequest(symbol=plan["short_call"].symbol, side=OrderSide.SELL, ratio_qty=1),
        OptionLegRequest(symbol=plan["long_call"].symbol, side=OrderSide.BUY, ratio_qty=1),
    ]
    limit_price = round(max(plan["net_credit"] - CREDIT_BUFFER, 0.01), 2)

    req = LimitOrderRequest(
        qty=plan["qty"],
        order_class=OrderClass.MLEG,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
        legs=legs,
    )
    order = trade_client.submit_order(req)
    log.info(f"Submitted iron condor order id={order.id} qty={plan['qty']} limit_price={limit_price}")
    return order


def log_trade(plan, order=None, dry_run=False):
    """Appends one row per attempted trade to logs/trades.csv (the "entry" log).
    Actual outcomes (fills, settlement, realized P&L) are logged separately by
    0dte_settle_trades.py once the 0DTE position has expired, keyed by order_id."""
    LOG_DIR.mkdir(exist_ok=True)
    is_new = not TRADE_LOG_CSV.exists()
    with open(TRADE_LOG_CSV, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow([
                "date", "timestamp", "underlying", "spot", "iv", "em",
                "short_put_symbol", "short_put_strike",
                "long_put_symbol", "long_put_strike",
                "short_call_symbol", "short_call_strike",
                "long_call_symbol", "long_call_strike",
                "net_credit", "max_risk", "qty", "order_id", "dry_run",
            ])
        now = datetime.now(TIMEZONE)
        writer.writerow([
            now.date().isoformat(), now.isoformat(),
            UNDERLYING, f"{plan['spot']:.2f}", f"{plan['iv']:.4f}", f"{plan['em']:.2f}",
            plan["short_put"].symbol, plan["short_put"].strike_price,
            plan["long_put"].symbol, plan["long_put"].strike_price,
            plan["short_call"].symbol, plan["short_call"].strike_price,
            plan["long_call"].symbol, plan["long_call"].strike_price,
            f"{plan['net_credit']:.2f}", f"{plan['max_risk']:.2f}", plan["qty"],
            getattr(order, "id", ""), dry_run,
        ])


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="SPY 0DTE Iron Condor bot (Alpaca paper/live trading)")
    parser.add_argument("--dry-run", action="store_true", help="Compute and log the trade but do not submit it.")
    parser.add_argument("--force", action="store_true", help="Skip the market-open check (for manual testing).")
    parser.add_argument(
        "--date", metavar="YYYY-MM-DD",
        help="Test against a specific trading day's expiration instead of today's calendar date "
             "(useful on weekends/holidays, or to re-check a past day). Implies you should also pass --force.",
    )
    parser.add_argument(
        "--test-iv", type=float, metavar="0.15",
        help="Bypass live ATM-straddle quotes/IV-solve and live bid/ask credit calc entirely, using this "
             "fixed IV and Black-Scholes theoretical pricing instead. For structural testing when the "
             "market is closed or quotes are stale/empty (e.g. weekend testing against Monday's just-"
             "listed contracts). Forces --dry-run -- this mode never submits a real order.",
    )
    args = parser.parse_args()

    target_date = date.fromisoformat(args.date) if args.date else None

    if args.test_iv is not None and not args.dry_run:
        log.warning("--test-iv implies --dry-run (simulated pricing is never used to submit a real order).")
        args.dry_run = True

    trade_client, option_data_client, stock_data_client = get_clients()

    if not args.force and not market_is_open_today(trade_client):
        log.warning("Market is not open right now -- exiting without trading. Use --force to override for testing.")
        return

    plan = build_iron_condor(
        trade_client, option_data_client, stock_data_client,
        target_date=target_date, test_iv=args.test_iv,
    )
    if plan is None:
        return  # already logged why (e.g. risk budget can't afford even 1 contract today)

    if args.dry_run:
        log.info("--dry-run set: not submitting order.")
        log_trade(plan, order=None, dry_run=True)
        return

    order = submit_iron_condor(trade_client, plan)
    log_trade(plan, order=order, dry_run=False)


if __name__ == "__main__":
    main()
