"""
SPY swing (multi-week) Iron Condor bot (Alpaca paper/live trading).

Third strategy in this family, alongside 0dte_iron_condor_bot.py (same-day) and
weekend_iron_condor_bot.py (Friday->Monday). This one enters a longer-dated iron condor
-- targeting SWING_TARGET_DTE calendar days out (e.g. 45, or 21 to compare) -- and holds
it for days/weeks rather than hours, until it hits a profit target, a stop loss, or
expiration.

This is intentionally ONE bot with a configurable entry DTE, not two separate 45-day and
21-day bots: point SWING_TARGET_DTE at whichever you want to test (e.g. run one instance
with SWING_TARGET_DTE=45, and separately compare 21 by changing the setting, or by running
a second copy of this repo with a different .env) -- 45 and 21 aren't tied together as one
strategy here (no automatic "enter at 45, force-close at 21" behavior).

Why this is architecturally different from the other two bots:
  - Unlike 0DTE (same-day) or weekend (fixed Friday->Monday gap), there's no fixed
    calendar relationship between "today" and the target expiration -- it's found by
    searching Alpaca's listed SPY expirations for whichever one lands closest to
    today + SWING_TARGET_DTE calendar days (see get_chain_near_target_dte()).
  - Since a position can stay open for weeks, running this bot daily via cron would open
    a NEW overlapping position every single day unless it checks first. main() gates
    entry on there being no other currently-open (unsettled, non-dry-run) swing position
    -- see has_open_position(). This is the key behavioral difference from the other two
    bots, which don't need this check (0DTE always starts flat each morning; weekend
    self-gates on the calendar gap instead).
  - Only a single T (calendar time-to-close) convention is used here -- unlike
    weekend_iron_condor_bot.py's dual calendar-T/trading-hours-T split (which exists
    specifically to model a short, deliberate weekend-arbitrage bet), a 45-day hold isn't
    trying to exploit a specific weekend-decay mismatch, it's just a standard
    Black-Scholes-priced credit spread over real elapsed time. Uses
    weekend_time.year_fraction_to_close_calendar() and get_trading_calendar()/
    session_close_dt() for the expiration's real close time -- these are generic
    calendar utilities, not specific to the weekend strategy despite living in that file.

Strategy:
  1. Pull SPY spot price and find the listed expiration nearest to today + SWING_TARGET_DTE.
  2. Solve implied volatility off the ATM straddle (Black-Scholes / Brent's method) using
     real calendar time-to-close.
  3. Expected Move: EM = spot * IV * sqrt(T)  (same formula as the other two bots).
  4. Short strikes  = spot +/- (SWING_EM_MULTIPLIER * EM)
  5. Long strikes   = short strike +/- (SWING_WING_FRACTION * EM)
  6. Submit a single 4-leg MLEG limit order (sell iron condor) on Alpaca -- but only if no
     other swing position is currently open.

IMPORTANT -- read before relying on this:
  - Run this on paper for a while (as you already do with the other two) before trusting
    it with real money -- a 45-day hold means it takes far longer to accumulate a
    meaningful sample of completed trades than the 0DTE bot does.
  - Exit management (profit target, stop loss) is handled by the companion
    swing_monitor_and_exit.py script, which -- unlike the other two monitors -- needs to
    keep running every trading day for the whole life of the position, not just one day.
  - Alpaca does not support SPX/XSP index options -- this trades SPY, same as the other two.
  - Defaults to paper trading -- see ALPACA_PAPER in .env before ever pointing this at a
    live account.
"""

import argparse
import csv
import math
import os
from datetime import datetime, date, time as dtime, timedelta
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
    session_open_dt,
    session_close_dt,
    year_fraction_to_close_calendar,
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
# All prefixed SWING_ (except shared account-level feed/rate settings) so this strategy
# can be sized/tuned entirely independently of the 0DTE and weekend strategies.
# --------------------------------------------------------------------------
load_dotenv()

UNDERLYING = os.getenv("UNDERLYING", "SPY")

TARGET_DTE = int(os.getenv("SWING_TARGET_DTE", "45"))  # calendar days out to target -- try 45, or 21 to compare
EXPIRATION_WINDOW_DAYS = int(os.getenv("SWING_EXPIRATION_WINDOW_DAYS", "7"))  # how far to search around the target for a real listed expiration

EM_MULTIPLIER = float(os.getenv("SWING_EM_MULTIPLIER", "1.25"))
WING_FRACTION = float(os.getenv("SWING_WING_FRACTION", "0.5"))
QTY = int(os.getenv("SWING_QTY", "1"))  # ceiling on contracts/leg -- actual qty is sized down to fit the risk budget below
RISK_FREE_RATE = float(os.getenv("RISK_FREE_RATE", "0.05"))

# Wider than the 0DTE/weekend default on purpose -- at 45 DTE, EM is a much larger
# fraction of spot (time value compounds with sqrt(T)), so the strike window needs to be
# wide enough to contain strikes that can be 10%+ away from spot in a normal IV
# environment. Too narrow and nearest_contract() will silently pick a strike much closer
# than the real target, understating the intended short-strike distance.
STRIKE_RANGE_PCT = float(os.getenv("SWING_STRIKE_RANGE_PCT", "0.15"))
CREDIT_BUFFER = float(os.getenv("SWING_CREDIT_BUFFER", "0.05"))

_max_risk_usd_env = os.getenv("SWING_MAX_RISK_PER_TRADE_USD")
_max_risk_pct_env = os.getenv("SWING_MAX_RISK_PER_TRADE_PCT")
MAX_RISK_PER_TRADE_USD = float(_max_risk_usd_env) if _max_risk_usd_env else None
MAX_RISK_PER_TRADE_PCT = float(_max_risk_pct_env) if _max_risk_pct_env else None  # fraction, e.g. 0.02 = 2%
if MAX_RISK_PER_TRADE_USD is None and MAX_RISK_PER_TRADE_PCT is None:
    MAX_RISK_PER_TRADE_USD = 500.0

# INFORMATIONAL ONLY -- see the identical comment in 0dte_iron_condor_bot.py. Read here
# purely to log how much smaller a cleanly-firing stop loss would be versus the
# max-theoretical-loss figure qty is actually sized against; does not affect sizing. The
# real, behavior-controlling copy lives in swing_monitor_and_exit.py. Especially relevant
# here: the monitor only checks every ~15 minutes and not at all overnight/over any of
# the weekends a 21-45 day hold spans, so treat the gap below as safety margin, not spare capacity.
STOP_LOSS_PCT_INFO = float(os.getenv("SWING_STOP_LOSS_PCT", "2.00"))

LOG_DIR = Path(__file__).parent / "logs"
TRADE_LOG_CSV = LOG_DIR / "swing_trades.csv"
CLOSED_EARLY_CSV = LOG_DIR / "swing_closed_early.csv"
OUTCOMES_CSV = LOG_DIR / "swing_trade_outcomes.csv"

STOCK_DATA_FEED = DataFeed(os.getenv("STOCK_DATA_FEED", "iex"))
OPTION_DATA_FEED = OptionsFeed(os.getenv("OPTION_DATA_FEED", "indicative"))

log = get_logger("swing_iron_condor_bot")


# --------------------------------------------------------------------------
# Black-Scholes helpers
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
    """Same dollar/percent resolution as the other two bots' compute_risk_budget(), just
    reading this strategy's own SWING_MAX_RISK_PER_TRADE_USD/PCT."""
    pct_budget = None
    if MAX_RISK_PER_TRADE_PCT is not None:
        account = trade_client.get_account()
        equity = float(account.equity)
        pct_budget = equity * MAX_RISK_PER_TRADE_PCT
        log.info(f"Account equity: ${equity:,.2f} -> SWING_MAX_RISK_PER_TRADE_PCT ({MAX_RISK_PER_TRADE_PCT:.2%}) budget = ${pct_budget:,.2f}")

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


# --------------------------------------------------------------------------
# Entry gating -- the key behavioral difference from the other two bots
# --------------------------------------------------------------------------
def read_csv_rows(path):
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def has_open_position():
    """True if there's already a real (non-dry-run), submitted swing trade that hasn't
    been closed early (per swing_closed_early.csv) and hasn't been settled yet (per
    swing_trade_outcomes.csv). Since this bot can hold a position for weeks, running it
    daily via cron without this check would stack a new overlapping position on top of
    the existing one every single day -- unlike 0dte_iron_condor_bot.py (always flat each
    morning, since 0DTE positions can't survive past today) or weekend_iron_condor_bot.py
    (self-gates on the Friday-vs-any-other-day calendar gap instead)."""
    trades = [r for r in read_csv_rows(TRADE_LOG_CSV) if r.get("order_id") and str(r.get("dry_run", "")).lower() != "true"]
    if not trades:
        return False, None

    closed_ids = {r["order_id"] for r in read_csv_rows(CLOSED_EARLY_CSV) if r.get("close_status") == "closed"}
    settled_ids = {r["order_id"] for r in read_csv_rows(OUTCOMES_CSV)}

    for row in trades:
        if row["order_id"] in closed_ids or row["order_id"] in settled_ids:
            continue  # fully resolved -- doesn't block a new entry
        return True, row  # still open (or at least not yet known to be resolved)

    return False, None


# --------------------------------------------------------------------------
# Chain / expiration selection
# --------------------------------------------------------------------------
def get_chain_near_target_dte(trade_client, symbol, spot, today, target_dte, window_days):
    """Finds the listed SPY expiration closest to `today + target_dte` calendar days,
    searching within +/- window_days of that target to tolerate whichever exact
    expirations Alpaca actually has listed (SPY lists very densely, but not necessarily
    on the exact calendar day math would want). Returns (calls, puts, expiration_date)
    for whichever expiration ends up closest -- NOT necessarily forward-only like the
    0DTE/weekend bots' chain fetches, since the nearest listed date could fall on either
    side of the exact target."""
    target_date = today + timedelta(days=target_dte)
    window_start = target_date - timedelta(days=window_days)
    window_end = target_date + timedelta(days=window_days)
    min_strike = spot * (1 - STRIKE_RANGE_PCT)
    max_strike = spot * (1 + STRIKE_RANGE_PCT)

    calls_req = GetOptionContractsRequest(
        underlying_symbols=[symbol],
        status=AssetStatus.ACTIVE,
        type=ContractType.CALL,
        strike_price_gte=str(min_strike),
        strike_price_lte=str(max_strike),
        expiration_date_gte=window_start,
        expiration_date_lte=window_end,
    )
    puts_req = GetOptionContractsRequest(
        underlying_symbols=[symbol],
        status=AssetStatus.ACTIVE,
        type=ContractType.PUT,
        strike_price_gte=str(min_strike),
        strike_price_lte=str(max_strike),
        expiration_date_gte=window_start,
        expiration_date_lte=window_end,
    )
    all_calls = trade_client.get_option_contracts(calls_req).option_contracts
    all_puts = trade_client.get_option_contracts(puts_req).option_contracts

    if not all_calls or not all_puts:
        raise RuntimeError(
            f"No {symbol} option contracts found expiring within {window_days} days of "
            f"{target_date} (target: {target_dte} DTE from {today}) within "
            f"+/-{STRIKE_RANGE_PCT:.0%} of spot. Widen SWING_EXPIRATION_WINDOW_DAYS or "
            "SWING_STRIKE_RANGE_PCT."
        )

    call_expirations = {c.expiration_date for c in all_calls}
    put_expirations = {p.expiration_date for p in all_puts}
    common_expirations = call_expirations & put_expirations
    if not common_expirations:
        raise RuntimeError(f"No common call/put expiration found for {symbol} in the {window_days}-day window around {target_date}.")

    chosen_expiration = min(common_expirations, key=lambda d: abs((d - target_date).days))
    actual_dte = (chosen_expiration - today).days
    log.info(
        f"Target: {target_dte} DTE from {today} ({target_date}) -> nearest listed expiration "
        f"{chosen_expiration} ({actual_dte} actual DTE, {abs(actual_dte - target_dte)} day(s) off target)"
    )

    calls = [c for c in all_calls if c.expiration_date == chosen_expiration]
    puts = [p for p in all_puts if p.expiration_date == chosen_expiration]
    return calls, puts, chosen_expiration


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
def build_swing_iron_condor(trade_client, option_data_client, stock_data_client, entry_date=None, test_iv=None):
    now = datetime.now(TIMEZONE)
    today = entry_date or now.date()

    spot = get_spot_price(stock_data_client, UNDERLYING)
    log.info(f"{UNDERLYING} spot: {spot:.2f}")

    calls, puts, expiration_date = get_chain_near_target_dte(trade_client, UNDERLYING, spot, today, TARGET_DTE, EXPIRATION_WINDOW_DAYS)

    calendar = get_trading_calendar(trade_client, today, expiration_date)
    expiration_session = next((s for s in calendar if s.date == expiration_date), None)
    if expiration_session is None:
        raise RuntimeError(
            f"Expiration date {expiration_date} wasn't found in Alpaca's trading calendar -- "
            "unexpected, since it came from a real listed option expiration. Check connectivity/calendar data."
        )
    close_dt = session_close_dt(expiration_session)

    todays_session = next((s for s in calendar if s.date == today), None)
    if todays_session is not None:
        market_open_dt = session_open_dt(todays_session)
        market_close_dt = session_close_dt(todays_session)
        outside_hours = now < market_open_dt or now >= market_close_dt
    else:
        outside_hours = True
        market_open_dt = datetime.combine(today, dtime(9, 31), tzinfo=TIMEZONE)

    if outside_hours or test_iv is not None:
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

    T = year_fraction_to_close_calendar(reference_for_T, close_dt)
    log.info(f"T = {T:.6f}y (~{T*365:.1f} calendar days to {expiration_date})")

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

        iv_call = implied_volatility(call_mid, spot, float(atm_call.strike_price), T, RISK_FREE_RATE, "call")
        iv_put = implied_volatility(put_mid, spot, float(atm_put.strike_price), T, RISK_FREE_RATE, "put")

        ivs = [iv for iv in (iv_call, iv_put) if iv is not None]
        if not ivs:
            raise RuntimeError(
                "Could not solve implied volatility from the ATM straddle -- the bid/ask quotes were "
                "probably empty/stale. Pass --test-iv 0.15 (or your own estimate) to bypass live quotes "
                "and test the rest of the pipeline structurally."
            )
        iv = float(np.mean(ivs))
        log.info(f"ATM straddle mid: call={call_mid:.2f} put={put_mid:.2f} -> IV={iv:.1%}")

    em = spot * iv * math.sqrt(T)
    log.info(f"Expected Move (EM): {em:.2f}  ({em/spot:.2%} of spot)")

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
            bs_price(spot, sp_k, T, RISK_FREE_RATE, iv, "put")
            + bs_price(spot, sc_k, T, RISK_FREE_RATE, iv, "call")
            - bs_price(spot, lp_k, T, RISK_FREE_RATE, iv, "put")
            - bs_price(spot, lc_k, T, RISK_FREE_RATE, iv, "call")
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
        log.warning(
            f"Even 1 contract's risk (${risk_per_contract:.2f}) exceeds the risk budget "
            f"(${risk_budget:.2f}) -- skipping entry today, no order submitted. This usually just means "
            "a low-IV setup produced a tight condor whose risk/contract happens to exceed budget; if you "
            "want to trade through setups like this, raise SWING_MAX_RISK_PER_TRADE_USD/PCT. (If this "
            "fires often, also check whether SWING_EM_MULTIPLIER/SWING_WING_FRACTION are producing wider "
            "wings than intended.)"
        )
        return None

    qty = min(QTY, max_affordable_qty)
    max_risk = risk_per_contract * qty

    # INFORMATIONAL ONLY (see STOP_LOSS_PCT_INFO above) -- does not affect qty/max_risk.
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
        f"{utilization_pct:.1%} of the max-loss figure this trade is sized against. Remember this monitor only "
        f"checks every ~15 min and not at all overnight/over weekends this hold will span -- see README."
    )

    return {
        "spot": spot,
        "iv": iv,
        "em": em,
        "entry_date": today,
        "expiration_date": expiration_date,
        "target_dte": TARGET_DTE,
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
    log.info(f"Submitted swing iron condor order id={order.id} qty={plan['qty']} limit_price={limit_price}")
    return order


def log_trade(plan, order=None, dry_run=False):
    """Appends one row per attempted trade to logs/swing_trades.csv. Includes both
    target_dte (what was configured) and expiration_date (what was actually found/used)
    since they can differ by a few days depending on what's actually listed."""
    LOG_DIR.mkdir(exist_ok=True)
    is_new = not TRADE_LOG_CSV.exists()
    with open(TRADE_LOG_CSV, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow([
                "date", "expiration_date", "target_dte", "timestamp", "underlying", "spot", "iv", "em",
                "short_put_symbol", "short_put_strike",
                "long_put_symbol", "long_put_strike",
                "short_call_symbol", "short_call_strike",
                "long_call_symbol", "long_call_strike",
                "net_credit", "max_risk", "qty", "order_id", "dry_run",
            ])
        now = datetime.now(TIMEZONE)
        writer.writerow([
            plan["entry_date"].isoformat(), plan["expiration_date"].isoformat(), plan["target_dte"], now.isoformat(),
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
    parser = argparse.ArgumentParser(description="SPY swing (multi-week) Iron Condor bot (Alpaca paper/live trading)")
    parser.add_argument("--dry-run", action="store_true", help="Compute and log the trade but do not submit it.")
    parser.add_argument(
        "--force", action="store_true",
        help="Skip the market-open check AND the no-open-position gate (for manual testing).",
    )
    parser.add_argument(
        "--date", metavar="YYYY-MM-DD",
        help="Test as if entering on a specific date instead of today. Implies you should also pass --force.",
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

    if not args.dry_run:
        open_position, open_row = has_open_position()
        if open_position and not args.force:
            log.info(
                f"A swing position is already open (order_id={open_row['order_id']}, entered "
                f"{open_row['date']}, expires {open_row.get('expiration_date', '?')}) -- skipping new "
                "entry. This bot holds one position at a time; swing_monitor_and_exit.py / "
                "swing_settle_trades.py will close/settle it before a new one opens. Use --force to override."
            )
            return

    plan = build_swing_iron_condor(
        trade_client, option_data_client, stock_data_client,
        entry_date=entry_date, test_iv=args.test_iv,
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
