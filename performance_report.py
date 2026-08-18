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
from pathlib import Path

from bot_logging import get_logger

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
    },
    "weekend": {
        "trade_log": LOG_DIR / "weekend_trades.csv",
        "outcomes": LOG_DIR / "weekend_trade_outcomes.csv",
        "joined": LOG_DIR / "weekend_trade_performance.csv",
        "chart": LOG_DIR / "weekend_performance_equity_curve.png",
        "chart_title": "SPY Weekend (Fri->Mon) Iron Condor — Live Paper Trading Cumulative P&L",
    },
}


def read_csv_rows(path):
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def join_trades(paths):
    trades = {r["order_id"]: r for r in read_csv_rows(paths["trade_log"]) if r.get("order_id")}
    outcomes = read_csv_rows(paths["outcomes"])

    joined = []
    for o in outcomes:
        t = trades.get(o["order_id"])
        if not t:
            continue
        joined.append({
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
            "target_credit": t["net_credit"],
            "raw_filled_avg_price": o.get("raw_filled_avg_price", ""),
            "fill_credit": o["fill_credit"],
            "spy_close": o["spy_close"],
            "settlement_value": o["settlement_value"],
            "gross_pnl": o.get("gross_pnl", ""),
            "fees": o.get("fees", ""),
            "realized_pnl": o["realized_pnl"],
            "notes": o["notes"],
        })
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
        entry_script = "0dte_iron_condor_bot.py" if args.strategy == "0dte" else "weekend_iron_condor_bot.py"
        settle_script = "0dte_settle_trades.py" if args.strategy == "0dte" else "weekend_settle_trades.py"
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
