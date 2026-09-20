"""
Performance report for the live (paper) SPY iron condor runs -- covers EITHER the 0DTE
strategy or the weekend (Friday->Monday) strategy, selected via --strategy.

For --strategy 0dte (the default, for backwards compatibility with existing usage):
  Joins logs/trades.csv (entry log, written by 0dte_iron_condor_bot.py) with
  logs/trade_outcomes.csv (settlement log, written by 0dte_settle_trades.py).
  Writes logs/trade_performance.csv and logs/performance_equity_curve.png.

For --strategy weekend:
  Joins logs/weekend_trades.csv (entry log, written by weekend_iron_condor_bot.py) with
  logs/weekend_trade_outcomes.csv (settlement log, written by weekend_settle_trades.py).
  Writes logs/weekend_trade_performance.csv and logs/weekend_performance_equity_curve.png.

Each strategy's numbers are entirely separate -- run this once per strategy (e.g. once
for each) rather than expecting a single combined P&L, since they have different risk
budgets, different holding periods, and are meant to be evaluated on their own merits.

Run this anytime — e.g. weekly, or after your 3-month collection window — to see
how the live results compare to the earlier backtest.
"""

import argparse
import csv
import os
from pathlib import Path

from dotenv import load_dotenv

from bot_logging import get_logger

load_dotenv()
log = get_logger("performance_report")

BASE = Path(__file__).parent
LOG_DIR = BASE / "logs"

STRATEGIES = {
    "0dte": {
        "trade_log": LOG_DIR / "trades.csv",
        "outcomes": LOG_DIR / "trade_outcomes.csv",
        "joined": LOG_DIR / "trade_performance.csv",
        "chart": LOG_DIR / "performance_equity_curve.png",
        "chart_title": "SPY 0DTE Iron Condor — Live Paper Trading Cumulative P&L",
        "stop_loss_env": "STOP_LOSS_PCT",
        "stop_loss_default": "1.20",
    },
    "weekend": {
        "trade_log": LOG_DIR / "weekend_trades.csv",
        "outcomes": LOG_DIR / "weekend_trade_outcomes.csv",
        "joined": LOG_DIR / "weekend_trade_performance.csv",
        "chart": LOG_DIR / "weekend_performance_equity_curve.png",
        "chart_title": "SPY Weekend (Fri->Mon) Iron Condor — Live Paper Trading Cumulative P&L",
        "stop_loss_env": "WEEKEND_STOP_LOSS_PCT",
        "stop_loss_default": "1.20",
    },
    "swing": {
        "trade_log": LOG_DIR / "swing_trades.csv",
        "outcomes": LOG_DIR / "swing_trade_outcomes.csv",
        "joined": LOG_DIR / "swing_trade_performance.csv",
        "chart": LOG_DIR / "swing_performance_equity_curve.png",
        "chart_title": "SPY Swing Iron Condor — Live Paper Trading Cumulative P&L",
        "stop_loss_env": "SWING_STOP_LOSS_PCT",
        "stop_loss_default": "2.00",
    },
}


def current_stop_loss_pct(paths):
    """Reads the CURRENTLY CONFIGURED stop-loss percentage for this strategy (same env
    var the matching *_monitor_and_exit.py reads). Used only to compute the informational
    max-loss-vs-stop-loss comparison below -- if you've changed this setting over time,
    older rows will be shown against today's value, not whatever was actually configured
    when each trade was placed."""
    return float(os.getenv(paths["stop_loss_env"], paths["stop_loss_default"]))


def read_csv_rows(path):
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _sizing_comparison(t, stop_loss_pct):
    """Computes, from data ALREADY in the entry log (strikes + target credit + qty), how
    the max-theoretical-loss figure qty was actually sized against compares to what a
    cleanly-firing stop-loss would realize -- see the "Contract sizing vs. stop-loss"
    README section and the entry bots' "[sizing info]" log line (which shows this same
    comparison at trade time, using whatever stop-loss pct was configured THEN -- this
    recomputes it retroactively using the CURRENTLY configured pct, which may differ from
    what was actually set when older trades were placed). Returns a dict of blank strings
    if any required field is missing/non-numeric (e.g. an errored/unfilled row) rather
    than raising.
    """
    blank = {"max_loss_per_contract": "", "stop_loss_loss_per_contract": "", "risk_utilization_pct": ""}
    try:
        short_put = float(t["short_put_strike"])
        long_put = float(t["long_put_strike"])
        short_call = float(t["short_call_strike"])
        long_call = float(t["long_call_strike"])
        net_credit = float(t["net_credit"])
        qty = int(float(t.get("qty", 1) or 1))
    except (KeyError, ValueError, TypeError):
        return blank

    max_wing_width = max(short_put - long_put, long_call - short_call)
    max_loss_per_contract = (max_wing_width - net_credit) * 100
    if max_loss_per_contract <= 0:
        return blank

    stop_loss_loss_per_contract = stop_loss_pct * net_credit * 100
    return {
        "max_loss_per_contract": f"{max_loss_per_contract:.2f}",
        "stop_loss_loss_per_contract": f"{stop_loss_loss_per_contract:.2f}",
        "risk_utilization_pct": f"{stop_loss_loss_per_contract / max_loss_per_contract:.4f}",
    }


def join_trades(paths):
    trades = {r["order_id"]: r for r in read_csv_rows(paths["trade_log"]) if r.get("order_id")}
    outcomes = read_csv_rows(paths["outcomes"])
    stop_loss_pct = current_stop_loss_pct(paths)

    joined = []
    for o in outcomes:
        t = trades.get(o["order_id"])
        if not t:
            continue
        row = {
            "date": o["date"],
            "expiration_date": o.get("expiration_date", o["date"]),
            "order_id": o["order_id"],
            "status": o["status"],
            "spot_open": t["spot"],
            "iv": t["iv"],
            "em": t["em"],
            "short_put": t["short_put_strike"],
            "long_put": t["long_put_strike"],
            "short_call": t["short_call_strike"],
            "long_call": t["long_call_strike"],
            "qty": t.get("qty", ""),
            "target_credit": t["net_credit"],
            "raw_filled_avg_price": o.get("raw_filled_avg_price", ""),
            "fill_credit": o["fill_credit"],
            "spy_close": o["spy_close"],
            "settlement_value": o["settlement_value"],
            "gross_pnl": o.get("gross_pnl", ""),
            "fees": o.get("fees", ""),
            "realized_pnl": o["realized_pnl"],
            "notes": o["notes"],
        }
        row.update(_sizing_comparison(t, stop_loss_pct))
        joined.append(row)
    joined.sort(key=lambda r: r["date"])
    return joined


def summarize(joined, paths):
    filled = [r for r in joined if r["status"] == "filled" and r["realized_pnl"] not in ("", None)]
    not_filled = [r for r in joined if r["status"] != "filled"]

    log.info(f"Total logged attempts: {len(joined)}  |  Filled: {len(filled)}  |  Not filled/errored: {len(not_filled)}")

    if not filled:
        log.info("No settled, filled trades yet — nothing to summarize.")
        return

    pnls = [float(r["realized_pnl"]) for r in filled]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    n = len(pnls)

    total_pnl = sum(pnls)
    win_rate = len(wins) / n
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    avg_fill_credit = sum(float(r["fill_credit"]) for r in filled) / n * 100
    total_fees = sum(float(r["fees"]) for r in filled if r.get("fees") not in ("", None))

    equity, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    log.info(f"Date range:            {filled[0]['date']} to {filled[-1]['date']}")
    log.info(f"Trades:                {n}")
    log.info(f"Total realized P&L:    ${total_pnl:,.2f}")
    log.info(f"Win rate:              {win_rate:.1%}  ({len(wins)}W / {len(losses)}L)")
    log.info(f"Avg fill credit:       ${avg_fill_credit:,.2f} /contract")
    log.info(f"Total fees paid:       ${total_fees:,.2f}  (already netted into P&L above)")
    log.info(f"Avg win:               ${avg_win:,.2f}")
    log.info(f"Avg loss:              ${avg_loss:,.2f}")
    log.info(f"Max drawdown:          ${max_dd:,.2f}")
    log.info(f"Final cumulative P&L:  ${equity:,.2f}")

    utilizations = [
        float(r["risk_utilization_pct"]) for r in filled if r.get("risk_utilization_pct") not in ("", None)
    ]
    if utilizations:
        avg_util = sum(utilizations) / len(utilizations)
        stop_loss_pct = current_stop_loss_pct(paths)
        log.info(
            f"Avg risk utilization:  {avg_util:.1%}  (stop-loss-implied loss vs. max-loss-sized "
            f"risk, at today's {stop_loss_pct:.0%} stop-loss setting -- see 'Contract sizing vs. "
            f"stop-loss' in README). NOTE: this recomputes every row against the CURRENTLY "
            f"configured stop-loss pct, not whatever was actually set when each trade was placed, "
            f"so treat it as a rough today's-settings snapshot rather than a precise historical figure."
        )

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from datetime import datetime

        dates = [datetime.strptime(r["date"], "%Y-%m-%d") for r in filled]
        eq_curve, running = [], 0.0
        for p in pnls:
            running += p
            eq_curve.append(running)

        fig, ax = plt.subplots(figsize=(9, 4.5))
        ax.plot(dates, eq_curve, color="#16a34a", linewidth=1.8)
        ax.axhline(0, color="#888", linewidth=0.8)
        ax.set_title(paths["chart_title"])
        ax.set_ylabel("Cumulative P&L ($)")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        fig.autofmt_xdate()
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(paths["chart"], dpi=150)
        log.info(f"Saved equity curve chart to {paths['chart']}")
    except ImportError:
        log.warning("matplotlib not installed — skipping chart; `pip install matplotlib` to enable it")
    except Exception as e:
        # Broad on purpose: the numeric summary above is the important output and has
        # already been logged/written by this point. A chart-rendering failure (e.g. a
        # corrupted matplotlib font cache -- `FT_Open_Face ... broken file` -- or any
        # other rendering issue) should never take down the rest of the report. See the
        # README for the font-cache fix if you hit this.
        log.warning(f"Could not render/save the equity curve chart ({e!r}) — skipping it; the numbers above are unaffected.")


def main():
    parser = argparse.ArgumentParser(description="Performance report for live iron condor trading (0DTE or weekend strategy).")
    parser.add_argument(
        "--strategy", choices=sorted(STRATEGIES.keys()), default="0dte",
        help="Which strategy's logs to report on (default: 0dte).",
    )
    args = parser.parse_args()
    paths = STRATEGIES[args.strategy]

    joined = join_trades(paths)
    if not joined:
        entry_script = f"{args.strategy}_iron_condor_bot.py"
        settle_script = f"{args.strategy}_settle_trades.py"
        log.info(f"No settled '{args.strategy}' trades found yet. Run {entry_script} then {settle_script} first.")
        return

    with open(paths["joined"], "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(joined[0].keys()))
        writer.writeheader()
        writer.writerows(joined)
    log.info(f"Wrote {len(joined)} joined rows to {paths['joined']}")

    summarize(joined, paths)


if __name__ == "__main__":
    main()
