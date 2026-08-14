# SPY 0DTE Iron Condor Bot (Alpaca Paper/Live Trading)

Sells a same-day-expiration iron condor on SPY at market open, sized off an
Expected Move (EM) calculated from the IV formula:

```
EM = spot * IV * sqrt(DTE / 365)
```

- IV is solved from the ATM straddle mid-price (Black-Scholes / Brent's method) — no paid
  options-data plan required.
- Short strikes = spot +/- 1.25 * EM (configurable)
- Long strikes (wings) = short strike +/- 0.5 * EM (configurable, scales with EM)
- Submits one 4-leg MLEG limit order (sell iron condor) via Alpaca's paper trading API.

## Why SPY, not SPX

Alpaca does not currently support SPX (or XSP) index options — only equity/ETF options,
confirmed via their docs and open feature requests. SPY is the closest tradable proxy
(~1/10th the price, physically settled instead of cash-settled). If Alpaca ships index
options support later, the strike/EM logic in `iron_condor_bot.py` carries over directly —
you'd just swap `UNDERLYING` and update contract-symbol assumptions.

## What this script does NOT do

- **No exit management on its own.** `iron_condor_bot.py` only opens the position — see
  `monitor_and_exit.py` below for the companion script that watches it and closes early
  on a profit target or stop loss.
- **Defaults to paper trading.** Test thoroughly before ever flipping to live (see
  "Going live" below):
  1. Run with `--dry-run` repeatedly first and sanity-check the strikes/credit it computes.
  2. Run for real in paper trading, small qty, and manually confirm fills/positions in the
     Alpaca dashboard.
  3. Only then consider scheduling it to run unattended.
  4. Only after that has run unattended for a while and you trust it, consider `ALPACA_PAPER=false`.

## Setup

```bash
cd iron_condor_bot          # wherever you place these files
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and paste your Alpaca PAPER API key + secret (Alpaca dashboard -> Paper Trading -> API Keys)
```

## Going live

All three trading scripts (`iron_condor_bot.py`, `monitor_and_exit.py`, `settle_trades.py`)
resolve their Alpaca credentials and paper/live mode through a shared `alpaca_config.py`,
controlled by one setting in `.env`:

```
ALPACA_PAPER=true   # default -- unset, or anything other than an explicit false-like
                     # value ("false", "0", "no", "off", "live"), stays in paper mode
```

To go live:

1. In your Alpaca dashboard, apply for options trading approval on the **live** account
   (Home page → "apply for options trading", next to "Add Funds") -- this is separate
   from paper trading, which gets full option-strategy access automatically. Per FINRA
   Rule 2360, every account needs this approval before its first live options trade.
2. Generate a new API key pair from the **live** account (Home/Account → API Keys) --
   paper and live keys are entirely separate credentials.
3. Fund the live account with at least enough buying power to cover your configured
   risk budget (`MAX_RISK_PER_TRADE_USD` / `MAX_RISK_PER_TRADE_PCT` x `QTY`).
4. In `.env`, either swap `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` to the new live keys, or
   (safer, lets you flip back and forth without re-editing keys) set
   `ALPACA_PAPER_API_KEY`/`ALPACA_PAPER_SECRET_KEY` to your paper keys and
   `ALPACA_LIVE_API_KEY`/`ALPACA_LIVE_SECRET_KEY` to your live keys -- these take
   priority over the plain `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` for whichever mode is active.
5. Set `ALPACA_PAPER=false`.
6. Before trusting it unattended, run each script manually once (e.g.
   `python3 iron_condor_bot.py --dry-run --force`) and check the console/log output opens
   with `Mode: PAPER trading` or the loud `!!! LIVE TRADING MODE !!!` warning as expected
   -- confirm it says what you think it should say before letting cron touch real money.
7. Strongly consider sizing down from whatever you were running in paper while you confirm
   live fills/behavior match what paper showed you (`QTY`, `MAX_RISK_PER_TRADE_USD`, or
   `MAX_RISK_PER_TRADE_PCT` -- see "Config knobs" below). Paper fills are simulated and
   don't necessarily reflect real slippage/liquidity on multi-leg combo orders.

To go back to paper, just set `ALPACA_PAPER=true` again (or remove the line, since that's
the default).

## Full daily schedule (all steps, end to end)

Four scripts, three of them scheduled, one run manually whenever you want a read on
performance:

| # | Time (ET) | Script | Purpose |
|---|---|---|---|
| 1 | ~9:31 AM, daily | `iron_condor_bot.py` | Opens today's iron condor. Logs to `logs/trades.csv`. |
| 2 | 9:35 AM – 3:55 PM, every 1-2 min | `monitor_and_exit.py` | Watches the open position; closes early at 80% profit or 120% loss. Logs to `logs/closed_early.csv`. |
| 3 | ~4:15 PM, daily (after close) | `settle_trades.py` | Settles the day's trade(s) — uses the real early-close price if #2 fired, otherwise computes settlement from the close price. Appends to `logs/trade_outcomes.csv`. |
| 4 | Anytime (not scheduled) | `performance_report.py` | Joins the logs and prints win rate / P&L / drawdown. Run manually whenever you want a check-in. |

Run these in order once manually first (with `--dry-run`/`--force` as needed — see the
sections below) to confirm each one works before automating any of it. Once confirmed,
this single cron block runs the full pipeline unattended, weekdays only:

```
31 9 * * 1-5     cd /path/to/iron_condor_bot && .venv/bin/python iron_condor_bot.py   >> logs/cron.log 2>&1
*/1 9-15 * * 1-5 cd /path/to/iron_condor_bot && .venv/bin/python monitor_and_exit.py  >> logs/cron.log 2>&1
15 16 * * 1-5    cd /path/to/iron_condor_bot && .venv/bin/python settle_trades.py     >> logs/cron.log 2>&1
```

Notes on that block:
- Line 1 fires at 9:31 (not 9:30) since quotes are often thin in the first seconds after
  the open — see "Scheduling the entry job" below for why.
- Line 2 fires every minute from 9am to 3:59pm, but `monitor_and_exit.py` itself checks
  the market clock and a `MONITOR_END_TIME` cutoff (3:55pm ET default) internally, so
  it's a harmless no-op outside real trading hours or once too close to the close.
- Line 3 fires once at 4:15pm, after the close and after `monitor_and_exit.py`'s last
  useful run for the day.
- This assumes your machine's system timezone is already `America/New_York`. If it runs
  in UTC (common for cloud VMs), use the timezone-aware APScheduler alternative in
  "Scheduling the entry job" below instead — it can run all three jobs from one script.

**Entry timing matters.** `iron_condor_bot.py` is designed to enter once near 9:31 AM
ET, with the full trading day still ahead. Expected Move scales with `sqrt(time
remaining until close)`, so if you run it for real (no `--test-iv`) later in the day,
it correctly uses the smaller *real* remaining time — EM shrinks a lot as the day goes
on, which can push the short strikes close enough to the money that the wing width
rounds up to SPY's $1 minimum increment, and the bid/ask spread on that thin, near-ATM
spread can eat the whole credit (or flip it slightly negative, which the bot correctly
refuses to submit rather than trading a degenerate position). That's expected behavior,
not a bug, if you manually test mid-day. To see a consistent, full-day-equivalent test
result regardless of what time you actually run it, use `--test-iv` (e.g. `--test-iv
0.15`) — it always simulates a 9:31 AM entry for the T/IV/EM math, not the real current
time.
- `performance_report.py` is intentionally not on this list — run it by hand whenever you
  want a read on results (see "Checking in on performance" below).

## Test it (no order submitted)

```bash
python3 iron_condor_bot.py --dry-run --force
```

`--force` skips the "is the market open" check so you can test outside market hours.
Drop `--force` once you're testing during real market hours. Check `logs/trades.csv` for
the row it would have logged, and read the console output for the computed spot/IV/EM/strikes.

## Run for real (paper account)

```bash
python3 iron_condor_bot.py
```

This checks that the market is actually open before doing anything, so it's safe to
schedule to fire slightly after 9:30 AM without it misfiring on holidays/weekends — though
you should still restrict the schedule to weekdays (see below).

## Scheduling the entry job

Quotes are often thin in the first seconds after the open; running at 9:31–9:32 ET tends
to get cleaner mid-prices. See "Full daily schedule" above for the complete cron block
covering all three jobs (entry, monitor, settle) together — this section just covers the
timezone-safe alternative in more detail.

**cron (Linux/macOS)** — works as shown above if your machine's system timezone is
already America/New_York.

If your server runs in UTC (common for cloud VMs), account for daylight saving manually,
or better, use a timezone-aware Python scheduler instead of raw cron. This one covers all
three scheduled jobs in a single always-on script:

```bash
pip install apscheduler
```

```python
from apscheduler.schedulers.blocking import BlockingScheduler
from zoneinfo import ZoneInfo
import subprocess

sched = BlockingScheduler(timezone=ZoneInfo("America/New_York"))

@sched.scheduled_job("cron", day_of_week="mon-fri", hour=9, minute=31)
def run_entry():
    subprocess.run(["python3", "iron_condor_bot.py"])

@sched.scheduled_job("cron", day_of_week="mon-fri", hour="9-15", minute="*/1")
def run_monitor():
    subprocess.run(["python3", "monitor_and_exit.py"])

@sched.scheduled_job("cron", day_of_week="mon-fri", hour=16, minute=15)
def run_settle():
    subprocess.run(["python3", "settle_trades.py"])

sched.start()
```

Run that scheduler script itself under something persistent (systemd service, `screen`/`tmux`,
or a small always-on VM) since it needs to stay running to fire the daily jobs.

**Windows Task Scheduler** — create three daily triggers (9:31 AM, every 1 min from 9:35
AM to 3:55 PM, and 4:15 PM), each with action = run the matching script's
`.venv\Scripts\python.exe <script>.py`, "Start in" set to the project folder.

## Logging

All four scripts log through a shared `bot_logging.py` module instead of `print()`.
Each script gets its own rotating log file:

- `logs/iron_condor_bot.log`
- `logs/monitor_and_exit.log`
- `logs/settle_trades.log`
- `logs/performance_report.log`

Files rotate automatically every `LOG_ROTATE_DAYS` (30 by default), keeping
`LOG_BACKUP_COUNT` (6 by default) old rotated files before the oldest is deleted —
old files are renamed with a date suffix (e.g. `iron_condor_bot.log.2026-08-14`), so
nothing is silently overwritten mid-rotation. Console output (visible when you run a
script by hand, and still captured by cron's `>> logs/cron.log 2>&1` redirect as a
fallback) uses the same format and level. Default level is `INFO`; override with
`LOG_LEVEL=DEBUG`/`WARNING`/`ERROR` in `.env` if you want less or more noise. These
log files are separate from the CSV logs (`trades.csv`, `trade_outcomes.csv`,
`closed_early.csv`) — the CSVs are the structured data used for settlement and
reporting; the `.log` files are the human-readable run history/diagnostics.

## Config knobs (env vars, see `.env.example`)

| Variable | Default | Meaning |
|---|---|---|
| `UNDERLYING` | `SPY` | Symbol traded |
| `EM_MULTIPLIER` | `1.25` | Short strike distance = this * EM |
| `WING_FRACTION` | `0.5` | Long strike (wing) = short strike +/- this * EM |
| `QTY` | `1` | **Ceiling** on contracts/leg -- the actual qty submitted is sized down to fit the risk budget below, never trading more than this even if the budget would allow it |
| `RISK_FREE_RATE` | `0.05` | Used in the Black-Scholes IV solve |
| `STRIKE_RANGE_PCT` | `0.08` | How wide a strike window to pull from the chain around spot |
| `MAX_RISK_PER_TRADE_USD` | `500` (only if `MAX_RISK_PER_TRADE_PCT` is also unset) | Dollar risk budget. `qty = floor(risk_budget / risk_per_contract)`, capped at `QTY` |
| `MAX_RISK_PER_TRADE_PCT` | unset | Risk budget as a fraction of live account equity (e.g. `0.02` = 2%), pulled from Alpaca each run. If both this and `MAX_RISK_PER_TRADE_USD` are set, the **lower** of the two dollar amounts wins -- the `$` figure acts as a hard ceiling on the `%` figure |

Either way, the bot aborts instead of submitting if even 1 contract's risk exceeds the resolved budget.
| `CREDIT_BUFFER` | `0.05` | Shaved off the mid-price credit so the limit order is more likely to fill |

## Logging trade performance (for the 3-month paper run)

Four scripts now work together (same lineup as "Full daily schedule" above, with more
detail on each one below):

| Script | When to run | What it does |
|---|---|---|
| `iron_condor_bot.py` | ~9:31 AM ET, daily | Opens the trade. Logs the attempt to `logs/trades.csv`. |
| `monitor_and_exit.py` | Every 1-2 min, 9:35 AM - 3:55 PM ET | Watches today's open position(s); closes early on an 80% profit target or a 120% stop loss. Logs to `logs/closed_early.csv`. |
| `settle_trades.py` | ~4:15 PM ET, daily (after close) | Looks up the real fill for each unsettled order, pulls SPY's official close, computes the settlement value + realized P&L from the logged strikes, and appends to `logs/trade_outcomes.csv`. Automatically uses the actual exit price instead if `monitor_and_exit.py` closed the trade early. |
| `performance_report.py` | Anytime (e.g. weekly, or after your 3 months) | Joins the two logs, prints win rate / total P&L / avg win-loss / max drawdown, and writes `logs/trade_performance.csv` + `logs/performance_equity_curve.png`. Same stats format as the earlier backtest, so you can compare live results to it directly. |

### Exit management: `monitor_and_exit.py`

Closes today's iron condor early if either threshold is hit, both measured against the
credit collected at entry (`entry_credit`, pulled from the actual fill, not the target):

- **Profit target (80% default):** once buying the combo back would cost 20% or less of
  the credit collected, submit a closing order (buy back the shorts, sell the longs).
- **Stop loss (120% default):** once buying the combo back would cost 220% or more of the
  credit collected (i.e. a loss of more than 120% of the credit), submit a closing order
  more aggressively (bigger price buffer, since a passive limit defeats the point of a
  stop). Both percentages and both buffers are configurable — see `.env.example`.

**Design: run this every 1-2 minutes via cron, not as one long-lived process.** Each
invocation is stateless except for `logs/closed_early.csv`, which is what makes repeated
invocations safe — it will never submit a second closing order for a position it has
already closed, or already has a closing order pending for. If a submitted closing order
sits unfilled for more than `PENDING_CLOSE_TIMEOUT_MIN` (10 min default), it's canceled
and resubmitted at a more aggressive price, up to `MAX_ESCALATIONS` times (5 default) —
past that it stops touching the order and prints a "CHECK MANUALLY" line so you notice it
in the cron log rather than silently chasing the market forever.

```bash
python3 monitor_and_exit.py --dry-run --force   # see what it would do, no orders submitted
```

See "Full daily schedule" above for the cron line (runs every minute 9am-4pm; the script
itself checks the market clock and a `MONITOR_END_TIME` cutoff — default 3:55 PM ET — so
it's a no-op outside real trading hours or once it's too close to the close to bother
opening a new early close).

### `logs/closed_early.csv`

One row per order that has triggered a profit-target or stop-loss close: `trigger`
(`profit_target`/`stop_loss`), `entry_credit`, `cost_to_close_at_trigger`, `profit_pct`,
`loss_pct`, `close_order_id`, `close_status` (`pending_close` → `closed`), `exit_debit`
(the real fill once the closing order fills), `estimated_pnl`, and `escalations` (how many
times a stale unfilled close was canceled/resubmitted).

**This feeds directly into `settle_trades.py`:** if a trade shows up here as `closed`,
`settle_trades.py` skips its normal hold-to-expiration settlement math (close price vs.
strikes) for that trade entirely and instead computes `realized_pnl` straight from
`entry_credit` vs. `exit_debit` — the real economics of what actually happened. This also
means that any past trade that was in fact closed early (rather than held to expiration)
would have been settled incorrectly before this component existed — worth keeping in mind
if you ever revisit an old settled trade whose P&L looked off.

### `logs/trades.csv` (entry log)

One row per attempted trade: date, spot, IV, EM, each leg's option symbol *and* numeric
strike, target net credit, max risk, qty, order id, and whether it was a `--dry-run`.

### `logs/trade_outcomes.csv` (settlement log)

One row per settled trade: date, order id, order status, actual filled credit, SPY's
close that day, the computed settlement value of the 4 legs, `gross_pnl` (credit minus
settlement value, before fees), `fees` (real per-contract regulatory/exchange fees
pulled from Alpaca's account activity for that date, not a guessed schedule), the final
`realized_pnl` (gross P&L + fees -- this is the number that should match your Alpaca
dashboard), and notes (e.g. why a trade wasn't filled, or a fallback that happened).

Fees are pulled from `GET /account/activities`, summing every `activity_type == "FEE"`
entry for that date (unambiguous fee line items). This is deliberately **not** filtered
by option symbol, even though the fills/other-activity matching below is: Alpaca's real
regulatory/exchange pass-through fees (OCC Clearing, CAT, OPT TAF, ORF, OPT REG, etc.)
are frequently reported at the account/day level rather than tagged to a single symbol
-- e.g. "CAT fee for proceed of 8 trades" or "ORF fee for proceed of 80 contracts",
spanning everything traded that day. An earlier version of this filtered fees by symbol
along with everything else, which silently excluded nearly all of them and made every
trade look fee-free ($0.00) even on days with several dollars of real regulatory fees --
Alpaca's *own* commissions are $0 for options, but the regulatory pass-through fees are
real and separate from that. If more than one trade is logged on the same date, that
date's fee total is split evenly across them (noted in the `notes` column) since the
fee activity doesn't reliably indicate which specific trade it belongs to.

If your dashboard P&L still doesn't fully match `realized_pnl` after this, check the
`notes` column -- any other non-fill account activity found for those contracts (e.g.
assignment/exercise events, since SPY is physically settled) is listed there for you to
review, since those can carry their own costs beyond the fees captured above.

Settlement is computed directly from the logged strikes vs. the close price — it
doesn't rely on Alpaca to correctly report a closed P&L for expired contracts, so it
stays correct regardless of how Alpaca's own activity/position reporting handles
0DTE expiration.

**Before trusting this over 3 months of real (paper) money:** the very first thing
`settle_trades.py` does is read `order.filled_avg_price` and assume it's the net
credit per combo, matching the sign convention used everywhere else in these scripts.
Confirm that assumption against a real filled order in your Alpaca paper dashboard
early on — if the sign or scale is off, every subsequent P&L number will be wrong in
the same direction, and it's much easier to catch that on day 1 than after 3 months.

### Scheduling the settlement job

See "Full daily schedule" above for the cron line — it fires at 4:15pm ET, after the
close and after `monitor_and_exit.py`'s last useful run for the day.

### Checking in on performance

```bash
python3 performance_report.py
```

Run this whenever you want a read on how the live paper run is doing — it's
non-destructive and safe to run as often as you like, including mid-way through your
3-month window.
