# SPY Iron Condor Bots (Alpaca Paper/Live Trading)

Two independent iron-condor strategies, sharing the same underlying (SPY), the same
Alpaca account/config plumbing, and the same overall design (sell a credit spread sized
off an Expected-Move calculation, then monitor and settle it) — but different holding
periods and different files:

| Strategy | Entry → Expiration | Scripts | Log files |
|---|---|---|---|
| **0DTE** | Same trading day | `0dte_iron_condor_bot.py`, `0dte_monitor_and_exit.py`, `0dte_settle_trades.py` | `logs/trades.csv`, `logs/closed_early.csv`, `logs/trade_outcomes.csv` |
| **Weekend** | Friday → Monday (or the next real trading session, if a holiday shifts it) | `weekend_iron_condor_bot.py`, `weekend_monitor_and_exit.py`, `weekend_settle_trades.py` | `logs/weekend_trades.csv`, `logs/weekend_closed_early.csv`, `logs/weekend_trade_outcomes.csv` |

`performance_report.py` covers both — run it with `--strategy 0dte` (default) or
`--strategy weekend`.

> **Migrating from the old unprefixed scripts?** If you were previously running
> `iron_condor_bot.py` / `monitor_and_exit.py` / `settle_trades.py` directly, those files
> still work exactly as before, but are superseded by the `0dte_`-prefixed copies above
> (same code, same log files — just a clearer name now that a second strategy exists).
> Update your cron/Task Scheduler entries to point at the new `0dte_*.py` filenames, confirm
> a few runs look right, then delete the old unprefixed `.py` files yourself once you're
> confident — they're kept side by side here only as a safety net during the transition.

## The 0DTE strategy

Sells a same-day-expiration iron condor on SPY at market open, sized off an Expected
Move (EM) calculated from the IV formula:

```
EM = spot * IV * sqrt(T)          where T = time remaining until today's 4:00pm ET close
```

- IV is solved from the ATM straddle mid-price (Black-Scholes / Brent's method) — no paid
  options-data plan required.
- Short strikes = spot +/- 1.25 * EM (configurable via `EM_MULTIPLIER`)
- Long strikes (wings) = short strike +/- 0.5 * EM (configurable via `WING_FRACTION`)
- Submits one 4-leg MLEG limit order (sell iron condor) via Alpaca's paper trading API.

## The weekend strategy

Sells a Friday-entry iron condor that's held through Monday's close (or further out, if
a holiday shifts the next trading session) instead of exiting same-day. The idea: an
option's time value decays for every *calendar* day that passes, but the underlying can
only actually move on real *trading* days — a Friday-to-Monday hold collects roughly 3
calendar days of theta decay against only about 1 trading day of new price-movement risk.
That's a real, if likely partially arbitraged, edge.

The real added cost versus the 0DTE strategy is about 48 hours of **unmonitorable weekend
gap risk**: if SPY gaps hard over the weekend (news, macro data, a geopolitical event),
this position cannot be adjusted or exited until Monday's open, by which point it may
already be past a short strike. This strategy is not "free money" — it's trading one kind
of risk (intraday monitoring) for another (weekend gap exposure).

To actually capture the edge rather than just taking on the extra risk for nothing, this
uses **two different T (time-to-close) conventions** rather than one, worked out in
`weekend_time.py`:

- **Calendar T** (real elapsed time, weekend included) — used to solve IV off the ATM
  straddle and to price the net credit. This matches how the market actually prices
  time value across a weekend; a Monday-expiring option quoted on Friday already has
  ~2.5 extra calendar days of value baked in versus a same-day 0DTE option.
- **Trading-hours T** (weekend/holiday excluded entirely) — used *only* to size the
  expected move / strike width. The underlying can only move on real trading time, so
  strikes are sized off the ~1 trading day actually ahead, not the ~3 calendar days the
  weekend spans.

Using calendar T for everything would size strikes as if the weekend carried the same
price-movement risk as three trading days (too wide to capture much of the edge). Using
trading-hours T for pricing too would understate what the position is really worth
relative to what the market prices. The split is what's intended to capture the decay
edge — deliberately at the cost of tighter strikes than a naive calendar-T sizing would
give, which is exactly where the weekend gap risk above concentrates.

Both the target expiration date and the trading-hours-only time calculation come from
Alpaca's own trading calendar (`GetCalendarRequest`), not a hardcoded weekday check — so
a holiday-shortened week (e.g. entering before a 3-day weekend, or a Monday holiday
pushing expiration to Tuesday) is handled correctly automatically.

**This bot only makes sense to run on the last trading day before a real gap.** It
self-gates on this: if the next real trading session is less than `WEEKEND_MIN_GAP_DAYS`
(2 by default) calendar days away, it logs a warning and exits without trading. Use the
0DTE bot instead for a same/next-day hold.

**Exit management:** `weekend_monitor_and_exit.py` is designed to run **only during the
expiration day's market hours** (e.g. Monday 9:31am–3:55pm ET) — there is nothing to
monitor or do while markets are closed over the weekend itself, so don't schedule it to
run Saturday/Sunday. It resumes automatically Monday morning and finds Friday-opened
positions by querying `weekend_trades.csv`'s `expiration_date` column, so it doesn't need
to be told in advance which day that session actually falls on.

## Why SPY, not SPX

Alpaca does not currently support SPX (or XSP) index options — only equity/ETF options,
confirmed via their docs and open feature requests. SPY is the closest tradable proxy
(~1/10th the price, physically settled instead of cash-settled). If Alpaca ships index
options support later, the strike/EM logic in both bots carries over directly — you'd
just swap `UNDERLYING` and update contract-symbol assumptions.

## What these scripts do NOT do

- **No exit management bundled into the entry bot.** Each entry bot only opens the
  position — the companion `*_monitor_and_exit.py` script watches it and closes early on
  a profit target or stop loss.
- **Default to paper trading.** Test thoroughly before ever flipping to live (see
  "Going live" below):
  1. Run with `--dry-run` repeatedly first and sanity-check the strikes/credit computed.
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

All six trading scripts (both strategies' entry/monitor/settle scripts) resolve their
Alpaca credentials and paper/live mode through a shared `alpaca_config.py`, controlled by
one setting in `.env`:

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
   risk budget(s) (`MAX_RISK_PER_TRADE_USD`/`PCT` x `QTY` for 0DTE, and separately
   `WEEKEND_MAX_RISK_PER_TRADE_USD`/`PCT` x `WEEKEND_QTY` for the weekend strategy).
4. In `.env`, either swap `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` to the new live keys, or
   (safer, lets you flip back and forth without re-editing keys) set
   `ALPACA_PAPER_API_KEY`/`ALPACA_PAPER_SECRET_KEY` to your paper keys and
   `ALPACA_LIVE_API_KEY`/`ALPACA_LIVE_SECRET_KEY` to your live keys -- these take
   priority over the plain `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` for whichever mode is active.
5. Set `ALPACA_PAPER=false`.
6. Before trusting it unattended, run each script manually once (e.g.
   `python3 0dte_iron_condor_bot.py --dry-run --force`) and check the console/log output
   opens with `Mode: PAPER trading` or the loud `!!! LIVE TRADING MODE !!!` warning as
   expected -- confirm it says what you think it should say before letting cron touch
   real money.
7. Strongly consider sizing down from whatever you were running in paper while you confirm
   live fills/behavior match what paper showed you. Paper fills are simulated and don't
   necessarily reflect real slippage/liquidity on multi-leg combo orders.

To go back to paper, just set `ALPACA_PAPER=true` again (or remove the line, since that's
the default).

## Full daily schedule — 0DTE strategy

Four scripts, three of them scheduled, one run manually whenever you want a read on
performance:

| # | Time (ET) | Script | Purpose |
|---|---|---|---|
| 1 | ~9:31 AM, daily | `0dte_iron_condor_bot.py` | Opens today's iron condor. Logs to `logs/trades.csv`. |
| 2 | 9:35 AM – 3:55 PM, every 1-2 min | `0dte_monitor_and_exit.py` | Watches the open position; closes early at 80% profit or 120% loss. Logs to `logs/closed_early.csv`. |
| 3 | ~4:15 PM, daily (after close) | `0dte_settle_trades.py` | Settles the day's trade(s) — uses the real early-close price if #2 fired, otherwise computes settlement from the close price. Appends to `logs/trade_outcomes.csv`. |
| 4 | Anytime (not scheduled) | `performance_report.py --strategy 0dte` | Joins the logs and prints win rate / P&L / drawdown. Run manually whenever you want a check-in. |

Run these in order once manually first (with `--dry-run`/`--force` as needed — see the
sections below) to confirm each one works before automating any of it. Once confirmed,
this cron block runs the full 0DTE pipeline unattended, weekdays only:

```
31 9 * * 1-5     cd /path/to/iron_condor_bot && .venv/bin/python 0dte_iron_condor_bot.py   >> logs/cron.log 2>&1
*/1 9-15 * * 1-5 cd /path/to/iron_condor_bot && .venv/bin/python 0dte_monitor_and_exit.py  >> logs/cron.log 2>&1
15 16 * * 1-5    cd /path/to/iron_condor_bot && .venv/bin/python 0dte_settle_trades.py     >> logs/cron.log 2>&1
```

Notes on that block:
- Line 1 fires at 9:31 (not 9:30) since quotes are often thin in the first seconds after
  the open — see "Scheduling the entry job" below for why.
- Line 2 fires every minute from 9am to 3:59pm, but `0dte_monitor_and_exit.py` itself
  checks the market clock and a `MONITOR_END_TIME` cutoff (3:55pm ET default) internally,
  so it's a harmless no-op outside real trading hours or once too close to the close.
- Line 3 fires once at 4:15pm, after the close and after `0dte_monitor_and_exit.py`'s
  last useful run for the day.
- This assumes your machine's system timezone is already `America/New_York`. If it runs
  in UTC (common for cloud VMs), use the timezone-aware APScheduler alternative in
  "Scheduling the entry job" below instead — it can run all the jobs (both strategies)
  from one script.

**Entry timing matters.** `0dte_iron_condor_bot.py` is designed to enter once near 9:31 AM
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

### Test it (no order submitted)

```bash
python3 0dte_iron_condor_bot.py --dry-run --force
```

`--force` skips the "is the market open" check so you can test outside market hours.
Drop `--force` once you're testing during real market hours. Check `logs/trades.csv` for
the row it would have logged, and read the console/log output for the computed
spot/IV/EM/strikes.

### Run for real (paper account)

```bash
python3 0dte_iron_condor_bot.py
```

This checks that the market is actually open before doing anything, so it's safe to
schedule to fire slightly after 9:30 AM without it misfiring on holidays/weekends — though
you should still restrict the schedule to weekdays (see below).

## Full daily schedule — weekend strategy

Same four-script shape, but the entry happens once (Friday) and the monitor/settle steps
happen on the expiration day (normally the following Monday):

| # | Day / Time (ET) | Script | Purpose |
|---|---|---|---|
| 1 | ~9:31 AM Friday (self-gates to real weekend-gap days — see below) | `weekend_iron_condor_bot.py` | Opens the weekend iron condor. Logs to `logs/weekend_trades.csv` (includes an `expiration_date` column). |
| 2 | 9:35 AM – 3:55 PM **on the expiration day** (usually Monday), every 1-2 min | `weekend_monitor_and_exit.py` | Watches the open position; closes early at 80% profit or 120% loss. Logs to `logs/weekend_closed_early.csv`. **Do not schedule this over the weekend itself.** |
| 3 | ~4:15 PM on the expiration day | `weekend_settle_trades.py` | Settles the trade(s) expiring that day. Appends to `logs/weekend_trade_outcomes.csv`. |
| 4 | Anytime (not scheduled) | `performance_report.py --strategy weekend` | Joins the weekend logs and prints win rate / P&L / drawdown. |

Cron block (runs every weekday, but the entry bot only actually trades on days with a
real weekend/holiday gap ahead — see "Self-gating" below — and the monitor/settle scripts
naturally no-op on days with nothing expiring):

```
31 9 * * 1-5     cd /path/to/iron_condor_bot && .venv/bin/python weekend_iron_condor_bot.py   >> logs/cron.log 2>&1
*/1 9-15 * * 1-5 cd /path/to/iron_condor_bot && .venv/bin/python weekend_monitor_and_exit.py  >> logs/cron.log 2>&1
15 16 * * 1-5    cd /path/to/iron_condor_bot && .venv/bin/python weekend_settle_trades.py     >> logs/cron.log 2>&1
```

**Self-gating (why it's safe to schedule this every weekday):** `weekend_iron_condor_bot.py`
checks the real gap to the next trading session (via Alpaca's calendar) before doing
anything. On a normal Tuesday, the next session is the very next day — a 1-day gap, below
the `WEEKEND_MIN_GAP_DAYS` (2) threshold — so it logs a warning and exits without trading.
On a Friday (or the last trading day before *any* multi-day gap, e.g. a holiday-extended
weekend), the gap is naturally >= 2 days, so it proceeds. This means the same cron line
scheduled every weekday automatically only fires for real on the days the strategy is
meant for, with no separate day-of-week logic needed in the crontab itself.

`weekend_monitor_and_exit.py` and `weekend_settle_trades.py` similarly no-op harmlessly
on any day where `weekend_trades.csv` has no row with a matching `expiration_date` — so
scheduling them every weekday (rather than trying to compute "which weekday is actually
Monday" in the crontab) is the simplest correct setup, at the cost of a few wasted
no-op invocations on non-expiration days.

### Test it (no order submitted)

```bash
python3 weekend_iron_condor_bot.py --dry-run --force
```

`--force` skips both the market-open check and the weekend-gap self-gate, so you can test
the strikes/credit math on any day, not just Fridays. Check `logs/weekend_trades.csv` for
the row it would have logged.

### Run for real (paper account)

```bash
python3 weekend_iron_condor_bot.py
```

Checks both the market-open clock and the weekend-gap self-gate before trading, so it's
safe to schedule every weekday morning without separately restricting it to Fridays only.

## Scheduling notes (both strategies)

Quotes are often thin in the first seconds after the open; running at 9:31–9:32 ET tends
to get cleaner mid-prices. See the two "Full daily schedule" sections above for the
complete cron blocks.

**cron (Linux/macOS)** — works as shown above if your machine's system timezone is
already America/New_York.

If your server runs in UTC (common for cloud VMs), account for daylight saving manually,
or better, use a timezone-aware Python scheduler instead of raw cron. This one script
covers all six scheduled jobs (both strategies) in one always-on process:

```bash
pip install apscheduler
```

```python
from apscheduler.schedulers.blocking import BlockingScheduler
from zoneinfo import ZoneInfo
import subprocess

sched = BlockingScheduler(timezone=ZoneInfo("America/New_York"))

@sched.scheduled_job("cron", day_of_week="mon-fri", hour=9, minute=31)
def run_0dte_entry():
    subprocess.run(["python3", "0dte_iron_condor_bot.py"])

@sched.scheduled_job("cron", day_of_week="mon-fri", hour="9-15", minute="*/1")
def run_0dte_monitor():
    subprocess.run(["python3", "0dte_monitor_and_exit.py"])

@sched.scheduled_job("cron", day_of_week="mon-fri", hour=16, minute=15)
def run_0dte_settle():
    subprocess.run(["python3", "0dte_settle_trades.py"])

@sched.scheduled_job("cron", day_of_week="mon-fri", hour=9, minute=31)
def run_weekend_entry():
    subprocess.run(["python3", "weekend_iron_condor_bot.py"])  # self-gates to real gap days

@sched.scheduled_job("cron", day_of_week="mon-fri", hour="9-15", minute="*/1")
def run_weekend_monitor():
    subprocess.run(["python3", "weekend_monitor_and_exit.py"])  # no-ops on non-expiration days

@sched.scheduled_job("cron", day_of_week="mon-fri", hour=16, minute=15)
def run_weekend_settle():
    subprocess.run(["python3", "weekend_settle_trades.py"])  # no-ops on non-expiration days

sched.start()
```

Run that scheduler script itself under something persistent (systemd service, `screen`/`tmux`,
or a small always-on VM) since it needs to stay running to fire the daily jobs.

**Windows Task Scheduler** — create the equivalent daily triggers for each script (9:31 AM,
every 1 min from 9:35 AM to 3:55 PM, and 4:15 PM), each with action = run the matching
script's `.venv\Scripts\python.exe <script>.py`, "Start in" set to the project folder.

## Logging

All eight scripts (both strategies' entry/monitor/settle/report, plus `performance_report.py`
itself covering both) log through a shared `bot_logging.py` module instead of `print()`.
Each script gets its own rotating log file:

- `logs/0dte_iron_condor_bot.log`
- `logs/0dte_monitor_and_exit.log`
- `logs/0dte_settle_trades.log`
- `logs/weekend_iron_condor_bot.log`
- `logs/weekend_monitor_and_exit.log`
- `logs/weekend_settle_trades.log`
- `logs/performance_report.log`

(If you're migrating from the old unprefixed scripts, `logs/iron_condor_bot.log` /
`logs/monitor_and_exit.log` / `logs/settle_trades.log` from before still exist as
harmless historical archive — new runs go to the `0dte_`-prefixed files above.)

Files rotate automatically every `LOG_ROTATE_DAYS` (30 by default), keeping
`LOG_BACKUP_COUNT` (6 by default) old rotated files before the oldest is deleted —
old files are renamed with a date suffix (e.g. `0dte_iron_condor_bot.log.2026-08-14`), so
nothing is silently overwritten mid-rotation. Console output (visible when you run a
script by hand, and still captured by cron's `>> logs/cron.log 2>&1` redirect as a
fallback) uses the same format and level. Default level is `INFO`; override with
`LOG_LEVEL=DEBUG`/`WARNING`/`ERROR` in `.env` if you want less or more noise. These
log files are separate from the CSV logs (`trades.csv`/`weekend_trades.csv`,
`trade_outcomes.csv`/`weekend_trade_outcomes.csv`, `closed_early.csv`/`weekend_closed_early.csv`)
— the CSVs are the structured data used for settlement and reporting; the `.log` files
are the human-readable run history/diagnostics.

## Config knobs (env vars, see `.env.example`)

Shared (account-level, used by both strategies):

| Variable | Default | Meaning |
|---|---|---|
| `UNDERLYING` | `SPY` | Symbol traded |
| `RISK_FREE_RATE` | `0.05` | Used in the Black-Scholes IV solve |
| `STOCK_DATA_FEED` | `iex` (entry bots) / `sip` (settle scripts) | Alpaca stock data feed tier |
| `OPTION_DATA_FEED` | `indicative` | Alpaca option data feed tier |

0DTE strategy:

| Variable | Default | Meaning |
|---|---|---|
| `EM_MULTIPLIER` | `1.25` | Short strike distance = this * EM |
| `WING_FRACTION` | `0.5` | Long strike (wing) = short strike +/- this * EM |
| `QTY` | `1` | **Ceiling** on contracts/leg -- the actual qty submitted is sized down to fit the risk budget below, never trading more than this even if the budget would allow it |
| `STRIKE_RANGE_PCT` | `0.08` | How wide a strike window to pull from the chain around spot |
| `MAX_RISK_PER_TRADE_USD` | `500` (only if `MAX_RISK_PER_TRADE_PCT` is also unset) | Dollar risk budget. `qty = floor(risk_budget / risk_per_contract)`, capped at `QTY` |
| `MAX_RISK_PER_TRADE_PCT` | unset | Risk budget as a fraction of live account equity (e.g. `0.02` = 2%), pulled from Alpaca each run. If both this and `MAX_RISK_PER_TRADE_USD` are set, the **lower** of the two dollar amounts wins -- the `$` figure acts as a hard ceiling on the `%` figure |
| `CREDIT_BUFFER` | `0.05` | Shaved off the mid-price credit so the limit order is more likely to fill |

Either way, the bot aborts instead of submitting if even 1 contract's risk exceeds the
resolved budget. `0dte_monitor_and_exit.py` overrides (`PROFIT_TARGET_PCT`, `STOP_LOSS_PCT`,
`PROFIT_CLOSE_BUFFER`, `STOP_LOSS_BUFFER`, `MONITOR_END_TIME`, `PENDING_CLOSE_TIMEOUT_MIN`,
`RESUBMIT_BUFFER_STEP`, `MAX_ESCALATIONS`) are documented in `.env.example`.

Weekend strategy (entirely separate config, so it can be sized/tuned independently of the
0DTE strategy above):

| Variable | Default | Meaning |
|---|---|---|
| `WEEKEND_EM_MULTIPLIER` | `1.5` | Wider than the 0DTE default on purpose -- see "The weekend strategy" above |
| `WEEKEND_WING_FRACTION` | `0.5` | Same semantics as `WING_FRACTION` |
| `WEEKEND_QTY` | `1` | Same ceiling semantics as `QTY` |
| `WEEKEND_STRIKE_RANGE_PCT` | `0.08` | Same semantics as `STRIKE_RANGE_PCT` |
| `WEEKEND_CREDIT_BUFFER` | `0.05` | Same semantics as `CREDIT_BUFFER` |
| `WEEKEND_MIN_GAP_DAYS` | `2` | Minimum calendar-day gap to the next trading session for the bot to treat this as a real weekend entry (see "Self-gating" above) |
| `WEEKEND_MAX_RISK_PER_TRADE_USD` / `WEEKEND_MAX_RISK_PER_TRADE_PCT` | `500` if both unset | Separate risk budget bucket from the 0DTE strategy's, same dollar-ceiling-over-percent resolution |

`weekend_monitor_and_exit.py` overrides (`WEEKEND_PROFIT_TARGET_PCT`,
`WEEKEND_STOP_LOSS_PCT`, `WEEKEND_PROFIT_CLOSE_BUFFER`, `WEEKEND_STOP_LOSS_BUFFER`,
`WEEKEND_MONITOR_END_TIME`, `WEEKEND_PENDING_CLOSE_TIMEOUT_MIN`,
`WEEKEND_RESUBMIT_BUFFER_STEP`, `WEEKEND_MAX_ESCALATIONS`) mirror the 0DTE monitor's,
default to the same values, and are documented in `.env.example`.

## Exit management: `*_monitor_and_exit.py`

Closes the open iron condor early if either threshold is hit, both measured against the
credit collected at entry (`entry_credit`, pulled from the actual fill, not the target):

- **Profit target (80% default):** once buying the combo back would cost 20% or less of
  the credit collected, submit a closing order (buy back the shorts, sell the longs).
- **Stop loss (120% default):** once buying the combo back would cost 220% or more of the
  credit collected (i.e. a loss of more than 120% of the credit), submit a closing order
  more aggressively (bigger price buffer, since a passive limit defeats the point of a
  stop). Both percentages and both buffers are configurable — see `.env.example`.

**Design: run this every 1-2 minutes via cron, not as one long-lived process.** Each
invocation is stateless except for `logs/closed_early.csv` (0DTE) /
`logs/weekend_closed_early.csv` (weekend), which is what makes repeated invocations safe
— it will never submit a second closing order for a position it has already closed, or
already has a closing order pending for. If a submitted closing order sits unfilled for
more than `PENDING_CLOSE_TIMEOUT_MIN`/`WEEKEND_PENDING_CLOSE_TIMEOUT_MIN` (10 min default),
it's canceled and resubmitted at a more aggressive price, up to `MAX_ESCALATIONS`/
`WEEKEND_MAX_ESCALATIONS` times (5 default) — past that it stops touching the order and
logs a "CHECK MANUALLY" error so you notice it rather than silently chasing the market forever.

```bash
python3 0dte_monitor_and_exit.py --dry-run --force      # 0DTE: see what it would do, no orders submitted
python3 weekend_monitor_and_exit.py --dry-run --force   # weekend: same, for positions expiring today
```

See the "Full daily schedule" sections above for the cron lines (each runs every minute
9am-4pm; the scripts themselves check the market clock and a `MONITOR_END_TIME` cutoff —
default 3:55 PM ET — so each is a no-op outside real trading hours, once too close to the
close, or on a day with no matching open position).

### `logs/closed_early.csv` / `logs/weekend_closed_early.csv`

One row per order that has triggered a profit-target or stop-loss close: `trigger`
(`profit_target`/`stop_loss`), `entry_credit`, `cost_to_close_at_trigger`, `profit_pct`,
`loss_pct`, `close_order_id`, `close_status` (`pending_close` → `closed`), `exit_debit`
(the real fill once the closing order fills), `estimated_pnl`, and `escalations` (how many
times a stale unfilled close was canceled/resubmitted).

**This feeds directly into the matching settle script:** if a trade shows up here as
`closed`, `0dte_settle_trades.py`/`weekend_settle_trades.py` skips its normal
hold-to-expiration settlement math (close price vs. strikes) for that trade entirely and
instead computes `realized_pnl` straight from `entry_credit` vs. `exit_debit` — the real
economics of what actually happened.

### `logs/trades.csv` / `logs/weekend_trades.csv` (entry logs)

One row per attempted trade: date, spot, IV, EM, each leg's option symbol *and* numeric
strike, target net credit, max risk, qty, order id, and whether it was a `--dry-run`.
The weekend log additionally has an `expiration_date` column (separate from `date`, the
entry date), since `weekend_monitor_and_exit.py`/`weekend_settle_trades.py` need to know
the real expiration to query/settle correctly.

### `logs/trade_outcomes.csv` / `logs/weekend_trade_outcomes.csv` (settlement logs)

One row per settled trade: date (and, for the weekend log, `expiration_date`), order id,
order status, actual filled credit, SPY's close on the expiration date, the computed
settlement value of the 4 legs, `gross_pnl` (credit minus settlement value, before fees),
`fees` (real per-contract regulatory/exchange fees pulled from Alpaca's account activity,
not a guessed schedule), the final `realized_pnl` (gross P&L + fees -- this is the number
that should match your Alpaca dashboard), and notes (e.g. why a trade wasn't filled, or a
fallback that happened).

Fees are pulled from `GET /account/activities`, summing every `activity_type == "FEE"`
entry for the relevant date(s) (unambiguous fee line items) — for the weekend strategy,
that means BOTH the entry date and the expiration/close date, since a Friday-to-Monday
hold can generate real fee activity on either day (opening the 4 legs on Friday, then
possibly exercise/assignment activity on expiration, or a separate early-close date).
This is deliberately **not** filtered by option symbol, even though the fills/other-activity
matching is: Alpaca's real regulatory/exchange pass-through fees (OCC Clearing, CAT, OPT
TAF, ORF, OPT REG, etc.) are frequently reported at the account/day level rather than
tagged to a single symbol -- e.g. "CAT fee for proceed of 8 trades" or "ORF fee for
proceed of 80 contracts", spanning everything traded that day. Alpaca's *own* commissions
are $0 for options, but these regulatory pass-through fees are real and separate from
that. If more than one trade shares a fee date, that date's fee total is split evenly
across them (noted in the `notes` column) since the fee activity doesn't reliably
indicate which specific trade it belongs to.

If your dashboard P&L still doesn't fully match `realized_pnl` after this, check the
`notes` column -- any other non-fill account activity found for those contracts (e.g.
assignment/exercise events, since SPY is physically settled) is listed there for you to
review, since those can carry their own costs beyond the fees captured above.

Settlement is computed directly from the logged strikes vs. the close price — it
doesn't rely on Alpaca to correctly report a closed P&L for expired contracts, so it
stays correct regardless of how Alpaca's own activity/position reporting handles
expiration.

**Before trusting this with real money:** the very first thing each settle script does
is read `order.filled_avg_price` and assume it's the net credit per combo, matching the
sign convention used everywhere else in these scripts. Confirm that assumption against a
real filled order in your Alpaca paper dashboard early on — if the sign or scale is off,
every subsequent P&L number will be wrong in the same direction, and it's much easier to
catch that on day 1 than after months of runs.

### Scheduling the settlement jobs

See the "Full daily schedule" sections above for the cron lines — each fires at 4:15pm ET
on the relevant expiration day, after the close and after that strategy's monitor's last
useful run for the day.

## Checking in on performance

```bash
python3 performance_report.py                    # 0DTE strategy (default)
python3 performance_report.py --strategy weekend  # weekend strategy
```

Run either whenever you want a read on how that strategy's live paper run is doing — it's
non-destructive and safe to run as often as you like. Each writes its own joined CSV +
equity curve chart (`logs/trade_performance.csv` + `logs/performance_equity_curve.png` for
0DTE, `logs/weekend_trade_performance.csv` + `logs/weekend_performance_equity_curve.png`
for weekend) and prints win rate / total P&L / avg win-loss / max drawdown to the log.
The two strategies' numbers are kept separate rather than combined, since they have
different risk budgets, different holding periods, and are meant to be evaluated on their
own merits.
