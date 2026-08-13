"""
Performance report for the live (paper) SPY 0DTE iron condor runs.

Joins logs/trades.csv (entry log, written by iron_condor_bot.py) with
logs/trade_outcomes.csv (settlement log, written by settle_trades.py) and prints
the same style of summary stats used in the backtest, plus writes:
  - logs/trade_performance.csv   (the joined, per-trade table)
  - logs/performance_equity_curve.png

Run this anytime — e.g. weekly, or after your 3-month collection window — to see
how the live results compare to the earlier backtest.
"""

import csv
from pathlib import Path

BASE = Path(__file__).parent
LOG_DIR = BASE / "logs"
TRADE_LOG_CSV = LOG_DIR / "trades.csv"
OUTCOMES_CSV = LOG_DIR / "trade_outcomes.csv"
JOINED_CSV = LOG_DIR / "trade_performance.csv"
CHART_PNG = LOG_DIR / "performance_equity_curve.png"


def read_csv_rows(path):
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def join_trades():
    trades = {r["order_id"]: r for r in read_csv_rows(TRADE_LOG_CSV) if r.get("order_id")}
    outcomes = read_csv_rows(OUTCOMES_CSV)

    joined = []
    for o in outcomes:
        t = trades.get(o["order_id"])
        if not t:
            continue
        joined.append({
            "date": o["date"],
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


def summarize(joined):
    filled = [r for r in joined if r["status"] == "filled" and r["realized_pnl"] not in ("", None)]
    not_filled = [r for r in joined if r["status"] != "filled"]

    print(f"Total logged attempts: {len(joined)}  |  Filled: {len(filled)}  |  Not filled/errored: {len(not_filled)}")

    if not filled:
        print("No settled, filled trades yet — nothing to summarize.")
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

    print(f"Date range:            {filled[0]['date']} to {filled[-1]['date']}")
    print(f"Trades:                {n}")
    print(f"Total realized P&L:    ${total_pnl:,.2f}")
    print(f"Win rate:              {win_rate:.1%}  ({len(wins)}W / {len(losses)}L)")
    print(f"Avg fill credit:       ${avg_fill_credit:,.2f} /contract")
    print(f"Total fees paid:       ${total_fees:,.2f}  (already netted into P&L above)")
    print(f"Avg win:               ${avg_win:,.2f}")
    print(f"Avg loss:              ${avg_loss:,.2f}")
    print(f"Max drawdown:          ${max_dd:,.2f}")
    print(f"Final cumulative P&L:  ${equity:,.2f}")

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
        ax.set_title("SPY 0DTE Iron Condor — Live Paper Trading Cumulative P&L")
        ax.set_ylabel("Cumulative P&L ($)")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        fig.autofmt_xdate()
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(CHART_PNG, dpi=150)
        print(f"\nSaved equity curve chart to {CHART_PNG}")
    except ImportError:
        print("\n(matplotlib not installed — skipping chart; `pip install matplotlib` to enable it)")


def main():
    joined = join_trades()
    if not joined:
        print("No settled trades found yet. Run iron_condor_bot.py then settle_trades.py first.")
        return

    with open(JOINED_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(joined[0].keys()))
        writer.writeheader()
        writer.writerows(joined)
    print(f"Wrote {len(joined)} joined rows to {JOINED_CSV}\n")

    summarize(joined)


if __name__ == "__main__":
    main()
