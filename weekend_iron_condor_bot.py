"""
SPY weekend (Friday -> Monday) Iron Condor bot (Alpaca paper/live trading).

Sibling of 0dte_iron_condor_bot.py: instead of opening and closing a same-day 0DTE iron
condor, this opens a condor on the last trading session before a weekend (normally
Friday, but automatically shifts to e.g. Thursday if Friday is a market holiday, or
expires on Tuesday if Monday is a holiday -- see weekend_time.py) and holds it through to
the next trading session's close.

Why: an option's time value decays for every calendar day that passes, but the
underlying can only actually move on real trading days. A Friday-to-Monday hold collects
roughly 3 calendar days of theta decay against only about 1 trading day of new
price-movement risk -- a real, if partially arbitraged, edge. The real added cost versus
the 0DTE strategy is ~2 days of unmonitorable weekend gap risk: if SPY gaps hard over the
weekend (news, macro data, geopolitical event), this position cannot be adjusted or exited
until Monday's open, by which point it may already be past a short strike.

Strategy (see weekend_time.py for the full T-convention rationale):
  1. Pull SPY spot price and find the next real trading session after today via Alpaca's
     own trading calendar (handles holidays automatically -- no hardcoded weekday logic).
  2. Solve implied volatility off the ATM straddle for that expiration, using CALENDAR T
     (real elapsed time, weekend included) -- this matches how the market actually prices
     time value across the weekend.
  3. Expected Move: EM = spot * IV * sqrt(TRADING_HOURS_T), where TRADING_HOURS_T counts
     ONLY real trading-session time between now and expiration (excluding the weekend
     entirely). This intentionally sizes strikes tighter than calendar-T would, which is
     what's meant to capture the weekend-decay edge -- and is exactly what concentrates
     more of the position's risk into the weekend gap described above.
  4. Short strikes  = spot +/- (WEEKEND_EM_MULTIPLIER * EM)
  5. Long strikes   = short strike +/- (WEEKEND_WING_FRACTION * EM)
  6. Submit a single 4-leg MLEG limit order (sell iron condor) on Alpaca.

IMPORTANT -- read before relying on this:
  - This bot only makes sense to run on the last trading day before a real weekend/holiday
    gap. It self-gates on this: if the next real trading session is less than 2 calendar
    days away (e.g. you run it on a Tuesday), it logs a warning and exits without trading
    (override with --force for testing). Use 0dte_iron_condor_bot.py for same/next-day
    holds instead.
  - Exit management (profit target, stop loss) is handled by the companion
    weekend_monitor_and_exit.py script -- per the intended design, that monitor should NOT
    run over the weekend itself (markets are closed, there's nothing to do), and should
    resume Monday morning once trading opens.
  - Alpaca does not support SPX/XSP index options -- this trades SPY (an ETF proxy for
    SPX, ~1/10th price, physically settled) instead, same as the 0DTE bot.
  - Test extensively with --dry-run, then with real paper-account orders, before trusting
    it to run unattended over a real weekend.
  - Defaults to paper trading -- see ALPACA_PAPER in .env before ever pointing this at a
    live account.
"""

import argparse
import csv
import math
import os
from datetime import datetime, date, time as dtime
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from scipy.optimize import brentq
from scipy.stats import norm

from bot_logging import get_logger
from alpaca_config import ALPACA_PAPER, API_KEY, SECRET_KEY
from weekend_time import (
    TIMEZONE,
    get_trading_calendar,
    next_trading_session,
    session_open_dt,
    session_close_dt,
    year_fraction_to_close_calendar,
    year_fraction_to_close_trading_hours,
)

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
# Config -- tune these, or override via .env / environment variables.
# Prefixed WEEKEND_ (where it makes sense to size this strategy independently of
# 0dte_iron_condor_bot.py) -- STOCK_DATA_FEED/OPTION_DATA_FEED/RISK_FREE_RATE are shared
# account-level settings, not strategy-specific, so those reuse the same env vars as the
# 0DTE bot rather than needing their own WEEKEND_ copies.
# --------------------------------------------------------------------------
load_dotenv()

UNDERLYING = os.getenv("UNDERLYING", "SPY")

# Default is intentionally WIDER than the 0DTE bot's 1.25x default -- this strategy's
# strikes are sized off trading-hours-only T (see weekend_time.py), which already makes
# them tighter than a naive calendar-T sizing would for the same nominal multiplier. A
# higher multiplier here partially offsets that, since the un-monitorable weekend gap
# risk is exactly what a tighter strike would otherwise concentrate. Still tune this
# yourself -- it's a starting point, not a back-tested optimum.
EM_MULTIPLIER = float(os.getenv("WEEKEND_EM_MULTIPLIER", "1.5"))
WING_FRACTION = float(os.getenv("WEEKEND_WING_FRACTION", "0.5"))
QTY = float(os.getenv("WEEKEND_QTY", "1"))
QTY = int(QTY)  # ceiling on contracts/leg -- actual qty is sized down to fit the risk budget below
RISK_FREE_RATE = float(os.getenv("RISK_FREE_RATE", "0.05"))
STRIKE_RANGE_PCT = float(os.getenv("WEEKEND_STRIKE_RANGE_PCT", "0.08"))
CREDIT_BUFFER = float(os.getenv("WEEKEND_CREDIT_BUFFER", "0.05"))

# Minimum real calendar-day gap to the next trading session for this bot to consider
# itself "a real weekend entry" -- see the module docstring. 2 covers a plain Friday ->
# Monday weekend; it's naturally larger (3+) before a long weekend/holiday.
MIN_GAP_DAYS = int(os.getenv("WEEKEND_MIN_GAP_DAYS", "2"))

# Separate risk budget bucket from the 0DTE strategy's MAX_RISK_PER_TRADE_USD/PCT -- these
# are two independent strategies that may reasonably be sized differently (e.g. smaller
# here, given the added weekend gap risk). Same dollar-ceiling-over-percent resolution
# logic as the 0DTE bot; falls back to a flat $500 if neither is set.
_max_risk_usd_env = os.getenv("WEEKEND_MAX_RISK_PER_TRADE_USD")
_max_risk_pct_env = os.getenv("WEEKEND_MAX_RISK_PER_TRADE_PCT")
MAX_RISK_PER_TRADE_USD = float(_max_risk_usd_env) if _max_risk_usd_env else None
MAX_RISK_PER_TRADE_PCT = float(_max_risk_pct_env) if _max_risk_pct_env else None  # fraction, e.g. 0.02 = 2%
if MAX_RISK_PER_TRADE_USD is None and MAX_RISK_PER_TRADE_PCT is None:
    MAX_RISK_PER_TRADE_USD = 500.0

LOG_DIR = Path(__file__).parent / "logs"
TRADE_LOG_CSV = LOG_DIR / "weekend_trades.csv"  # separate from the 0DTE bot's trades.csv on purpose

# Shared account-level feed settings -- same env vars as 0dte_iron_condor_bot.py.
STOCK_DATA_FEED = DataFeed(os.getenv("STOCK_DATA_FEED", "iex"))
OPTION_DATA_FEED = OptionsFeed(os.getenv("OPTION_DATA_FEED", "indicative"))

log = get_logger("weekend_iron_condor_bot")


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
    """Same dollar/percent resolution as 0dte_iron_condor_bot.py's compute_risk_budget(),
    just reading this strategy's own WEEKEND_MAX_RISK_PER_TRADE_USD/PCT."""
    pct_budget = None
    if MAX_RISK_PER_TRADE_PCT is not None:
        account = trade_client.get_account()
        equity = float(account.equity)
        pct_budget = equity * MAX_RISK_PER_TRADE_PCT
        log.info(f"Account equity: ${equity:,.2f} -> WEEKEND_MAX_RISK_PER_TRADE_PCT ({MAX_RISK_PER_TRADE_PCT:.2%}) budget = ${pct_budget:,.2f}")

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


def get_chain_for_expiration(trade_client, symbol, spot, expiration_date):
    """Fetches the option chain for a SPECIFIC expiration date -- unlike the 0DTE bot's
    get_today_chain(), this does NOT fall forward to "nearest available" if that exact
    date isn't found, since expiration_date here is already the real next trading
    session's date straight from Alpaca's own calendar (via next_trading_session()) --
    if SPY doesn't have contracts listed for it, that's worth a loud error rather than
    silently substituting a different date."""
    min_strike = spot * (1 - STRIKE_RANGE_PCT)
    max_strike = spot * (1 + STRIKE_RANGE_PCT)

    calls_req = GetOptionContractsRequest(
        underlying_symbols=[symbol],
        status=AssetStatus.ACTIVE,
        type=ContractType.CALL,
        strike_price_gte=str(min_strike),
        strike_price_lte=str(max_strike),
        expiration_date=expiration_date,
    )
    puts_req = GetOptionContractsRequest(
        underlying_symbols=[symbol],
        status=AssetStatus.ACTIVE,
        type=ContractType.PUT,
        strike_price_gte=str(min_strike),
        strike_price_lte=str(max_strike),
        expiration_date=expiration_date,
    )
    calls = trade_client.get_option_contracts(calls_req).option_contracts
    puts = trade_client.get_option_contracts(puts_req).option_contracts

    if not calls or not puts:
        raise RuntimeError(
            f"No {symbol} option contracts found expiring exactly on {expiration_date} within "
            f"+/-{STRIKE_RANGE_PCT:.0%} of spot. Widen WEEKEND_STRIKE_RANGE_PCT, or confirm {symbol} "
            f"actually lists an expiration on {expiration_date} (check the Alpaca/broker chain directly)."
        )
    return calls, puts


def get_quotes(option_data_client, symbols):
    req = OptionLatestQuoteRequest(symbol_or_symbols=symbols, feed=OPTION_DATA_FEED)
    return option_data_client.get_option_latest_quote(req)


def mid(quote):
    return (float(quote.bid_price) + float(quote.ask_price)) / 2.0


def nearest_contract(contracts, target_strike):
    return min(contracts, key=lambda c: abs(float(c.strike_price) - target_strike))


# --------------------------------------------------------------------------
# Core strategy logic
# --------------------------------------------------------------------------
def build_weekend_iron_condor(trade_client, option_data_client, stock_data_client, entry_date=None, test_iv=None):
    """Mirrors 0dte_iron_condor_bot.py's build_iron_condor() structurally, but:
      - the expiration is the next real trading session after `entry_date` (or today),
        found via Alpaca's own calendar (weekend_time.next_trading_session), not "today";
      - IV solve / theoretical (--test-iv) pricing use CALENDAR T (weekend included);
      - EM/strike sizing uses TRADING-HOURS T (weekend excluded).
    See weekend_time.py's module docstring for the full rationale."""
    now = datetime.now(TIMEZONE)
    today = entry_date or now.date()

    next_session = next_trading_session(trade_client, today)
    expiration_date = next_session.date
    close_dt = session_close_dt(next_session)
    gap_days = (expiration_date - today).days

    spot = get_spot_price(stock_data_client, UNDERLYING)
    log.info(f"{UNDERLYING} spot: {spot:.2f}")
    log.info(f"Entry date: {today}  ->  next trading session / expiration: {expiration_date}  (gap: {gap_days} calendar day(s))")

    calls, puts = get_chain_for_expiration(trade_client, UNDERLYING, spot, expiration_date)

    # Trading-hours T needs every real session between entry and expiration (inclusive) --
    # normally just [today's session, expiration's session], but fetching the full range
    # means a multi-day holiday gap (e.g. entry Thursday before a Friday holiday, expiring
    # the following Monday) is still handled correctly with zero special-casing.
    calendar = get_trading_calendar(trade_client, today, expiration_date)
    todays_session = next((s for s in calendar if s.date == today), None)

    if todays_session is not None:
        market_open_dt = session_open_dt(todays_session)
        market_close_dt = session_close_dt(todays_session)
        outside_hours = now < market_open_dt or now >= market_close_dt
    else:
        # `today` isn't a real trading session in Alpaca's calendar at all (e.g. testing
        # with --date pointing at a Saturday) -- there's no real market-hours window to
        # compare against, so always simulate a 9:31am ET entry on that date instead.
        outside_hours = True
        market_open_dt = datetime.combine(today, dtime(9, 31), tzinfo=TIMEZONE)

    if outside_hours or test_iv is not None:
        # Same rationale as 0dte_iron_condor_bot.py's build_iron_condor(): simulate a
        # normal 9:31am ET entry rather than using the real wall-clock time, either
        # because we're genuinely outside real market hours (weekend/holiday/night) or
        # because --test-iv is explicitly a structural test that should be consistent
        # regardless of when it's actually run.
        reference_for_T = market_open_dt
        if outside_hours:
            log.warning(
                f"Current time ({now.strftime('%Y-%m-%d %H:%M %Z')}) is outside real market hours for "
                f"{today} -- simulating time-to-close as if it were 9:31 AM ET on {today} for T/IV/EM purposes."
            )
        else:
            log.warning(
                "--test-iv set: simulating time-to-close as if it were 9:31 AM ET (not the actual "
                "current time), so structural testing is consistent regardless of when you run it."
            )
    else:
        reference_for_T = now

    T_pricing = year_fraction_to_close_calendar(reference_for_T, close_dt)
    T_sizing = year_fraction_to_close_trading_hours(reference_for_T, close_dt, calendar)
    log.info(f"T (calendar, for IV/pricing) = {T_pricing:.6f}y (~{T_pricing*365:.2f} calendar days)")
    log.info(f"T (trading-hours, for EM/strikes) = {T_sizing:.6f}y (~{T_sizing*365*24:.2f} trading hours)")

    if test_iv is not None:
        iv = test_iv
        log.warning(
            f"--test-iv {iv:.1%} set: skipping live ATM quotes/IV-solve entirely -- using this fixed IV instead. "
            "Simulated, not from live market data."
        )
    else:
        atm_call = nearest_contract(calls, spot)
        atm_put = nearest_contract(puts, spot)

        quotes = get_quotes(option_data_client, [atm_call.symbol, atm_put.symbol])
        call_mid = mid(quotes[atm_call.symbol])
        put_mid = mid(quotes[atm_put.symbol])

        iv_call = implied_volatility(call_mid, spot, float(atm_call.strike_price), T_pricing, RISK_FREE_RATE, "call")
        iv_put = implied_volatility(put_mid, spot, float(atm_put.strike_price), T_pricing, RISK_FREE_RATE, "put")

        ivs = [iv for iv in (iv_call, iv_put) if iv is not None]
        if not ivs:
            raise RuntimeError(
                "Could not solve implied volatility from the ATM straddle -- the bid/ask quotes were "
                "probably empty/stale. Pass --test-iv 0.15 (or your own estimate) to bypass live quotes "
                "and test the rest of the pipeline structurally."
            )
        iv = float(np.mean(ivs))
        log.info(f"ATM straddle mid: call={call_mid:.2f} put={put_mid:.2f} -> IV={iv:.1%}")

    em = spot * iv * math.sqrt(T_sizing)
    log.info(f"Expected Move (EM, trading-hours T): {em:.2f}  ({em/spot:.2%} of spot)")

    short_put_target = spot - EM_MULTIPLIER * em
    short_call_target = spot + EM_MULTIPLIER * em
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
            bs_price(spot, sp_k, T_pricing, RISK_FREE_RATE, iv, "put")
            + bs_price(spot, sc_k, T_pricing, RISK_FREE_RATE, iv, "call")
            - bs_price(spot, lp_k, T_pricing, RISK_FREE_RATE, iv, "put")
            - bs_price(spot, lc_k, T_pricing, RISK_FREE_RATE, iv, "call")
        )
        log.warning("--test-iv set: net_credit is a Black-Scholes theoretical estimate, not from live quotes.")
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

    risk_budget = compute_risk_budget(trade_client)
    max_affordable_qty = math.floor(risk_budget / risk_per_contract)
    if max_affordable_qty < 1:
        raise RuntimeError(
            f"Even 1 contract's risk (${risk_per_contract:.2f}) exceeds the risk budget "
            f"(${risk_budget:.2f}) -- aborting. Raise WEEKEND_MAX_RISK_PER_TRADE_USD/PCT, or check "
            "whether WEEKEND_EM_MULTIPLIER/WEEKEND_WING_FRACTION are producing wider wings than intended."
        )

    qty = min(QTY, max_affordable_qty)
    max_risk = risk_per_contract * qty

    log.info(
        f"Net credit (mid): {net_credit:.2f}/contract | Risk/contract: ${risk_per_contract:.2f} | "
        f"Max affordable qty: {max_affordable_qty} (budget ${risk_budget:.2f}) | QTY cap: {QTY} | "
        f"Using qty={qty} | Max risk: ${max_risk:.2f}"
    )

    return {
        "spot": spot,
        "iv": iv,
        "em": em,
        "entry_date": today,
        "expiration_date": expiration_date,
        "short_put": short_put,
        "long_put": long_put,
        "short_call": short_call,
        "long_call": long_call,
        "net_credit": net_credit,
        "max_risk": max_risk,
        "risk_budget": risk_budget,
        "qty": qty,
    }


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
    log.info(f"Submitted weekend iron condor order id={order.id} qty={plan['qty']} limit_price={limit_price}")
    return order


def log_trade(plan, order=None, dry_run=False):
    """Appends one row per attempted trade to logs/weekend_trades.csv (kept separate
    from the 0DTE strategy's logs/trades.csv). Includes an expiration_date column (unlike
    the 0DTE log, where expiration == the entry date) since weekend_monitor_and_exit.py
    and weekend_settle_trades.py both need to know the real expiration to query/settle
    correctly. Actual outcomes are logged separately by weekend_settle_trades.py once the
    position has expired, keyed by order_id."""
    LOG_DIR.mkdir(exist_ok=True)
    is_new = not TRADE_LOG_CSV.exists()
    with open(TRADE_LOG_CSV, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow([
                "date", "expiration_date", "timestamp", "underlying", "spot", "iv", "em",
                "short_put_symbol", "short_put_strike",
                "long_put_symbol", "long_put_strike",
                "short_call_symbol", "short_call_strike",
                "long_call_symbol", "long_call_strike",
                "net_credit", "max_risk", "qty", "order_id", "dry_run",
            ])
        now = datetime.now(TIMEZONE)
        writer.writerow([
            plan["entry_date"].isoformat(), plan["expiration_date"].isoformat(), now.isoformat(),
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
    parser = argparse.ArgumentParser(description="SPY weekend (Friday->Monday) Iron Condor bot (Alpaca paper/live trading)")
    parser.add_argument("--dry-run", action="store_true", help="Compute and log the trade but do not submit it.")
    parser.add_argument(
        "--force", action="store_true",
        help="Skip the market-open check AND the weekend-gap check (for manual testing on any day).",
    )
    parser.add_argument(
        "--date", metavar="YYYY-MM-DD",
        help="Test as if entering on a specific date instead of today (useful on weekends/holidays, "
             "or to re-check a past entry). Implies you should also pass --force.",
    )
    parser.add_argument(
        "--test-iv", type=float, metavar="0.15",
        help="Bypass live ATM-straddle quotes/IV-solve and live bid/ask credit calc entirely, using this "
             "fixed IV and Black-Scholes theoretical pricing instead. Forces --dry-run.",
    )
    args = parser.parse_args()

    entry_date = date.fromisoformat(args.date) if args.date else None

    if args.test_iv is not None and not args.dry_run:
        log.warning("--test-iv implies --dry-run (simulated pricing is never used to submit a real order).")
        args.dry_run = True

    trade_client, option_data_client, stock_data_client = get_clients()

    if not args.force and not market_is_open_today(trade_client):
        log.warning("Market is not open right now -- exiting without trading. Use --force to override for testing.")
        return

    today = entry_date or datetime.now(TIMEZONE).date()
    next_session = next_trading_session(trade_client, today)
    gap_days = (next_session.date - today).days
    if gap_days < MIN_GAP_DAYS and not args.force:
        log.warning(
            f"Next trading session ({next_session.date}) is only {gap_days} day(s) after {today} -- "
            f"this isn't a weekend-spanning entry (need >= {MIN_GAP_DAYS} days; use "
            "0dte_iron_condor_bot.py for a same/next-day hold instead). Skipping. Use --force to override."
        )
        return

    plan = build_weekend_iron_condor(
        trade_client, option_data_client, stock_data_client,
        entry_date=entry_date, test_iv=args.test_iv,
    )

    if args.dry_run:
        log.info("--dry-run set: not submitting order.")
        log_trade(plan, order=None, dry_run=True)
        return

    order = submit_iron_condor(trade_client, plan)
    log_trade(plan, order=order, dry_run=False)


if __name__ == "__main__":
    main()
