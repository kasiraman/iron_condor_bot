"""
SPY 0DTE Iron Condor bot (Alpaca paper trading).

Strategy:
  1. Pull SPY spot price and today's (0DTE) option chain.
  2. Solve implied volatility off the ATM straddle (Black-Scholes / Brent's method).
  3. Expected Move:  EM = spot * IV * sqrt(DTE_fraction / 365)
     where DTE_fraction is the remaining fraction of the trading day (time now -> 4:00pm ET),
     expressed as a day-count, consistent with the IV solve's own T.
  4. Short strikes  = spot +/- (EM_MULTIPLIER * EM)      [default 1.25x]
  5. Long strikes   = short strike +/- (WING_FRACTION * EM)  [default 0.5x, i.e. wing width scales with EM]
  6. Submit a single 4-leg MLEG limit order (sell iron condor) on Alpaca paper trading.

IMPORTANT -- read before relying on this:
  - Alpaca does not yet support SPX/XSP index options (confirmed via their docs as of this writing).
    This bot trades SPY (an ETF proxy for SPX, ~1/10th price, physically settled) instead.
  - This script places ENTRY orders only. It does not manage exits (profit target, stop loss,
    or end-of-day close). 0DTE short options carry gap/pin risk into the close -- decide your
    exit/management plan before running this unattended.
  - Test extensively with --dry-run, then with real paper-account orders, before trusting it
    to run unattended.
  - Never point this at a live (non-paper) account without fully understanding the risk.
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

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

UNDERLYING = os.getenv("UNDERLYING", "SPY")
EM_MULTIPLIER = float(os.getenv("EM_MULTIPLIER", "1.25"))      # short strike = spot +/- EM_MULTIPLIER * EM
WING_FRACTION = float(os.getenv("WING_FRACTION", "0.5"))       # long strike = short +/- WING_FRACTION * EM
QTY = int(os.getenv("QTY", "1"))  # ceiling on contracts/leg -- actual qty is sized down to fit MAX_RISK_PER_TRADE_USD
RISK_FREE_RATE = float(os.getenv("RISK_FREE_RATE", "0.05"))
STRIKE_RANGE_PCT = float(os.getenv("STRIKE_RANGE_PCT", "0.08"))  # how wide a strike window to pull from the chain
MAX_RISK_PER_TRADE_USD = float(os.getenv("MAX_RISK_PER_TRADE_USD", "500"))  # qty = floor(this / risk-per-contract), capped at QTY
CREDIT_BUFFER = float(os.getenv("CREDIT_BUFFER", "0.05"))        # shave this off mid-credit to help the limit order fill
LOG_DIR = Path(__file__).parent / "logs"
TRADE_LOG_CSV = LOG_DIR / "trades.csv"

# Free/basic Alpaca accounts only get the IEX stock feed and "indicative" (not real-time
# OPRA) option data -- the SDK's defaults require a paid subscription and raise
# "subscription does not permit querying recent SIP data" otherwise. Override via .env
# (STOCK_DATA_FEED=sip / OPTION_DATA_FEED=opra) if you do have a paid plan.
STOCK_DATA_FEED = DataFeed(os.getenv("STOCK_DATA_FEED", "iex"))
OPTION_DATA_FEED = OptionsFeed(os.getenv("OPTION_DATA_FEED", "indicative"))

TIMEZONE = ZoneInfo("America/New_York")

log = get_logger("iron_condor_bot")


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
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set (check your .env file).")
    trade_client = TradingClient(api_key=API_KEY, secret_key=SECRET_KEY, paper=True)
    option_data_client = OptionHistoricalDataClient(api_key=API_KEY, secret_key=SECRET_KEY)
    stock_data_client = StockHistoricalDataClient(api_key=API_KEY, secret_key=SECRET_KEY)
    return trade_client, option_data_client, stock_data_client


def market_is_open_today(trade_client) -> bool:
    clock = trade_client.get_clock()
    return clock.is_open


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
    real trading days should NOT need --test-iv, since real quotes will be live."""
    now = datetime.now(TIMEZONE)
    spot = get_spot_price(stock_data_client, UNDERLYING)
    log.info(f"{UNDERLYING} spot: {spot:.2f}")

    calls, puts = get_today_chain(trade_client, UNDERLYING, spot, target_date=target_date)

    market_open_dt = now.replace(hour=9, minute=31, second=0, microsecond=0)
    market_close_dt = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now < market_open_dt or now >= market_close_dt:
        # Outside real market hours -- almost always means --force testing (weekend, holiday,
        # or just running this at night). The real wall-clock time-to-close is meaningless here
        # (negative/near-zero after the close), which used to collapse T to a 1-second floor and
        # blow up the IV solve into nonsense (e.g. 496% IV from a stale after-hours straddle
        # quote) -- producing strikes so close together they'd round to the same contract on one
        # side, and a broken (negative) net credit. Simulate a normal 9:31 AM ET entry instead so
        # --force testing behaves sanely no matter what time of day you actually run it. A real
        # scheduled run during market hours never hits this branch.
        reference_for_T = market_open_dt
        log.warning(
            f"Current time ({now.strftime('%H:%M %Z')}) is outside real market hours (9:30-16:00 ET) -- "
            "simulating time-to-close as if it were 9:31 AM ET for T/IV/EM purposes."
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
    max_affordable_qty = math.floor(MAX_RISK_PER_TRADE_USD / risk_per_contract)
    if max_affordable_qty < 1:
        raise RuntimeError(
            f"Even 1 contract's risk (${risk_per_contract:.2f}) exceeds MAX_RISK_PER_TRADE_USD "
            f"(${MAX_RISK_PER_TRADE_USD:.2f}) -- aborting. Raise MAX_RISK_PER_TRADE_USD, or check "
            "whether EM_MULTIPLIER/WING_FRACTION are producing wider wings than intended."
        )

    qty = min(QTY, max_affordable_qty)
    max_risk = risk_per_contract * qty

    log.info(
        f"Net credit (mid): {net_credit:.2f}/contract | Risk/contract: ${risk_per_contract:.2f} | "
        f"Max affordable qty: {max_affordable_qty} (budget ${MAX_RISK_PER_TRADE_USD:.2f}) | QTY cap: {QTY} | "
        f"Using qty={qty} | Max risk: ${max_risk:.2f}"
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
    log.info(f"Submitted iron condor order id={order.id} qty={plan['qty']} limit_price={limit_price}")
    return order


def log_trade(plan, order=None, dry_run=False):
    """Appends one row per attempted trade to logs/trades.csv (the "entry" log).
    Actual outcomes (fills, settlement, realized P&L) are logged separately by
    settle_trades.py once the 0DTE position has expired, keyed by order_id."""
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
    parser = argparse.ArgumentParser(description="SPY 0DTE Iron Condor bot (Alpaca paper trading)")
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

    if args.dry_run:
        log.info("--dry-run set: not submitting order.")
        log_trade(plan, order=None, dry_run=True)
        return

    order = submit_iron_condor(trade_client, plan)
    log_trade(plan, order=order, dry_run=False)


if __name__ == "__main__":
    main()
