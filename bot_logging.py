"""
Shared logging setup for the SPY 0DTE iron condor bot scripts.

Every script (iron_condor_bot.py, monitor_and_exit.py, settle_trades.py,
performance_report.py) calls get_logger(<script_name>) once at import time and uses
the returned logger instead of print(). Each script gets its own rotating log file
under logs/<script_name>.log, rotated automatically every LOG_ROTATE_DAYS (30 by
default) with LOG_BACKUP_COUNT (6 by default) old rotated files kept before the
oldest is deleted. Also logs to the console at the same level, so cron's existing
`>> logs/cron.log 2>&1` redirect still captures a duplicate copy -- useful as a
catch-all for anything that goes wrong before/outside logging itself (e.g. an
import error).

Why TimedRotatingFileHandler works correctly even though these are short-lived,
cron-invoked processes (not a long-running daemon): when the handler is constructed,
it checks the log file's own last-modified time on disk (if the file already exists)
to compute the next rollover point, rather than just using "now" -- so the rollover
schedule is effectively anchored to the log file itself and survives correctly across
many separate process invocations, not just within one long-running process.

Usage:
    from bot_logging import get_logger
    log = get_logger("monitor_and_exit")
"""

import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

LOG_DIR = Path(__file__).parent / "logs"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_ROTATE_DAYS = int(os.getenv("LOG_ROTATE_DAYS", "30"))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "6"))

_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


def get_logger(name: str) -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    logger = logging.getLogger(name)

    if logger.handlers:
        # Already configured (e.g. get_logger called more than once for the same
        # name) -- don't add duplicate handlers, which would duplicate every line.
        return logger

    logger.setLevel(LOG_LEVEL)

    file_handler = TimedRotatingFileHandler(
        filename=LOG_DIR / f"{name}.log",
        when="D",
        interval=LOG_ROTATE_DAYS,
        backupCount=LOG_BACKUP_COUNT,
    )
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(_FORMAT))
    logger.addHandler(console_handler)

    logger.propagate = False
    return logger
