"""
Shared paper/live credential resolver for the SPY 0DTE iron condor bot scripts.

Controlled by ALPACA_PAPER in .env (defaults to paper/true if unset or unrecognized --
safe by default). Alpaca issues a completely separate key pair for paper vs. live
accounts, so this also picks the right pair of credentials for whichever mode is active:

  - ALPACA_PAPER=true  (default) -> uses ALPACA_PAPER_API_KEY/ALPACA_PAPER_SECRET_KEY
    if set, otherwise falls back to the plain ALPACA_API_KEY/ALPACA_SECRET_KEY (so an
    existing .env with just those two vars keeps working unchanged).
  - ALPACA_PAPER=false -> REAL MONEY, REAL ORDERS. Uses ALPACA_LIVE_API_KEY/
    ALPACA_LIVE_SECRET_KEY if set, otherwise also falls back to the plain
    ALPACA_API_KEY/ALPACA_SECRET_KEY.

Setting separate ALPACA_PAPER_* / ALPACA_LIVE_* pairs (rather than reusing the plain
ALPACA_API_KEY/ALPACA_SECRET_KEY for whichever mode you're in) lets you keep both sets
of credentials in .env at once and flip ALPACA_PAPER back and forth freely -- e.g. to
run paper and live side by side, or to drop back to paper without re-editing keys.

Usage (same pattern in every script):
    from alpaca_config import ALPACA_PAPER, API_KEY, SECRET_KEY
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _is_paper(raw: str) -> bool:
    # Safe by default: anything other than an explicit "false-like" value stays paper.
    return raw.strip().lower() not in ("false", "0", "no", "off", "live")


ALPACA_PAPER = _is_paper(os.getenv("ALPACA_PAPER", "true"))

if ALPACA_PAPER:
    API_KEY = os.getenv("ALPACA_PAPER_API_KEY") or os.getenv("ALPACA_API_KEY")
    SECRET_KEY = os.getenv("ALPACA_PAPER_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")
else:
    API_KEY = os.getenv("ALPACA_LIVE_API_KEY") or os.getenv("ALPACA_API_KEY")
    SECRET_KEY = os.getenv("ALPACA_LIVE_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")
