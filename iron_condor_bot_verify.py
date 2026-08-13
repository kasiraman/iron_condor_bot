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
import logging
import math
import os
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from dotenv import load_dotenv
from scipy.optimize import brentq
from scipy.stats import norm

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

# --------------------------------------------------------------------------
# Config -- tune these, or override via .env / environment variables
# --------------------------------------------------------------------------
load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

UNDERLYING = os.getenv("UNDERLYING", "SPY")
EM_MULTIPLIER = float(os.getenv("EM_MULTIPLIER", "1.25"))      # short strike = spot +/- EM_MULTIPLIER * EM
WING_FRACTION = float(os.getenv("WING_FRACTION", "0.5"))       # long strike = short +/- WING_FRACTION * EM
QTY = int(os.getenv("QTY", "1"))
RISK_FREE_RATE = float(os.getenv("RISK_FREE_RATE", "0.05"))
STRIKE_RANGE_PCT = float(os.getenv("STRIKE_RANGE_PCT", "0.08"))  # how wide a strike window to pull from the chain
MAX_RISK_PER_TRADE_USD = float(os.getenv("MAX_RISK_PER_TRADE_USD", "500"))
CREDIT_BUFFER = float(os.getenv("CREDIT_BUFFER", "0.05"))        # shave this off mid-credit to help the limit order fill
LOG_DIR = Path(__file__).parent / "logs"
TRADE_LOG_CSV = LOG_DIR / "trades.csv"

TIMEZONE = ZoneInfo("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("iron_condor_bot")


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
    req = StockLatestTradeRequest(symbol_or_symbols=symbol)
    resp = stock_data_client.get_stock_latest_trade(req)
    return float(resp[symbol].price)


def get_today_chain(trade_client, symbol, spot, target_date=None):
    today = target_date or datetime.now(TIMEZONE).date()
    min_strike = spot * (1 - STRIKE_RANGE_PCT)
    max_strike = spot * (1 + STRIKE_RANGE_PCT)

    calls_req = GetOptionContractsRequest(
        underlying_symbols=[symbol],
        status=AssetStatus.ACTIVE,
        type=ContractType.CALL,
        strike_price_gte=str(min_strike),
        strike_price_lte=str(max_strike),
        expiration_date_gte=today,
        expiration_date_lte=today,
    )
    puts_req = GetOptionContractsRequest(
        underlying_symbols=[symbol],
        status=AssetStatus.ACTIVE,
        type=ContractType.PUT,
        strike_price_gte=str(min_strike),
        strike_price_lte=str(max_strike),
        expiration_date_gte=today,
        expiration_date_lte=today,
    )
    calls = trade_client.get_option_contracts(calls_req).option_contracts
    puts = trade_client.get_option_contracts(puts_req).option_contracts

    if not calls or not puts:
        raise RuntimeError(
            f"No 0DTE contracts found for {symbol} expiring {today} within +/-{STRIKE_RANGE_PCT:.0%} of spot. "
            "If today is a weekend/holiday there's simply no expiration dated today (spot price still "
            "resolves to Friday's close, which is why that part succeeded) -- pass --date YYYY-MM-DD "
            "with a real past/upcoming trading day to test the strike/EM logic, or widen STRIKE_RANGE_PCT."
        )
    return calls, puts


def get_quotes(option_data_client, symbols):
    req = OptionLatestQuoteRequest(symbol_or_symbols=symbols)
    quotes = option_data_client.get_option_latest_quote(req)
    return quotes


def mid(quote):
    return (float(quote.bid_price) + float(quote.ask_price)) / 2.0


def nearest_contract(contracts, target_strike):
    return min(contracts, key=lambda c: abs(float(c.strike_price) - target_strike))


# --------------------------------------------------------------------------
# Core strategy logic
# --------------------------------------------------------------------------
def build_iron_condor(trade_client, option_data_client, stock_data_client, target_date=None):
    now = datetime.now(TIMEZONE)
    spot = get_spot_price(stock_data_client, UNDERLYING)
    log.info(f"{UNDERLYING} spot: {spot:.2f}")

    calls, puts = get_today_chain(trade_client, UNDERLYING, spot, target_date=target_date)

    # ATM strike = closest available strike to spot (use whichever side has it; check both)
    atm_call = nearest_contract(calls, spot)
    atm_put = nearest_contract(puts, spot)

    quotes = get_quotes(option_data_client, [atm_call.symbol, atm_put.symbol])
    call_mid = mid(quotes[atm_call.symbol])
    put_mid = mid(quotes[atm_put.symbol])

    T = year_fraction_to_close(now)
    iv_call = implied_volatility(call_mid, spot, float(atm_call.strike_price), T, RISK_FREE_RATE, "call")
    iv_put = implied_volatility(put_mid, spot, float(atm_put.strike_price), T, RISK_FREE_RATE, "put")

    ivs = [iv for iv in (iv_call, iv_put) if iv is not None]
    if not ivs:
        raise RuntimeError("Could not solve implied volatility from the ATM straddle -- check quotes/liquidity.")
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

    leg_symbols = [short_put.symbol, long_put.symbol, short_call.symbol, long_call.symbol]
    leg_quotes = get_quotes(option_data_client, leg_symbols)

    net_credit = (
        float(leg_quotes[short_put.symbol].bid_price)
        + float(leg_quotes[short_call.symbol].bid_price)
        - float(leg_quotes[long_put.symbol].ask_price)
        - float(leg_quotes[long_call.symbol].ask_price)
    )

    put_wing_width = float(short_put.strike_price) - float(long_put.strike_price)
    call_wing_width = float(long_call.strike_price) - float(short_call.strike_price)
    max_wing_width = max(put_wing_width, call_wing_width)
    max_risk = (max_wing_width - net_credit) * 100 * QTY

    log.info(f"Net credit (mid): {net_credit:.2f}/contract | Max risk: ${max_risk:.2f} for qty={QTY}")

    if net_credit <= 0:
        raise RuntimeError(f"Computed net credit is non-positive ({net_credit:.2f}) -- aborting, check quotes.")
    if max_risk > MAX_RISK_PER_TRADE_USD:
        raise RuntimeError(
            f"Max risk ${max_risk:.2f} exceeds MAX_RISK_PER_TRADE_USD (${MAX_RISK_PER_TRADE_USD:.2f}) -- aborting."
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
        qty=QTY,
        order_class=OrderClass.MLEG,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
        legs=legs,
    )
    order = trade_client.submit_order(req)
    log.info(f"Submitted iron condor order id={order.id} limit_price={limit_price}")
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
            f"{plan['net_credit']:.2f}", f"{plan['max_risk']:.2f}", QTY,
            getattr(order, "id", ""), dry_run,
        ])


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="SPY 0DTE Iron Condor bot (Alpaca paper trading)")
    parser.add_argument("--dry-run", action="store_true", help="Compute and log the trade but do not submit it.")
    parser.add_argument("--force", action="