# zdte-bot — 0DTE ETF options bot for Alpaca

A small, readable Python bot that trades **same-day-expiry (0DTE) options** on big, liquid ETFs
(**SPY, QQQ, IWM** by default) through [Alpaca](https://alpaca.markets). It runs on paper trading
by default, has a no-orders dry-run mode, and only needs Python plus your API keys.

> **Risk warning.** 0DTE options can lose 100% of their value within minutes. This is
> educational software with no guarantee of profit, and nothing here is financial advice.
> Run it on **paper** for weeks before you even consider real money, and never trade money
> you can't afford to lose.

---

## What it does

Both strategies start from the same signal. From 9:30 ET the bot records each ETF's high and
low (the *opening range*), then waits for a 1-minute bar to close outside it, confirmed by VWAP.
Pick a strategy with `mode:` in `config.yaml`:

### `mode: long_options` (default): buy a call or put
1. Uses a 15-minute opening range. A breakout above it buys a **call**; a breakout below buys a **put**.
2. It picks the 0DTE contract nearest **0.40 delta** that has a tight spread and a sensible premium,
   and sizes the trade to `max_position_usd`.
3. Exits: **+50% take profit**, **−35% stop**, and a **trailing stop** (once up 30%, exit if it falls 20% from its high).

### `mode: credit_spread`: sell a defined-risk spread (needs options level 3)
Modelled on the most widely published 0DTE bot, the "60-minute ORB":
1. Uses a 60-minute opening range. It skips the day if the range is narrower than 0.2%. No entries after 12:00.
2. Breakout **above** → **sell a put spread** with the short strike at the **range low** ($2 wide).
   Breakout **below** → **sell a call spread** with the short strike at the **range high**.
   The trade wins unless price reverses through the *entire* opening range.
3. Sized so the worst case, `(width − credit) × 100 × qty`, stays within `max_risk_usd`.
4. Exits: keep **50% of the credit**, a **stop when the loss equals the credit**, or the
   underlying **breaks the short strike**.

Both legs go out as one Alpaca multi-leg order, so you're never left with a naked short.

### The trading day

| Time (ET) | What happens |
|---|---|
| 9:30 | Opening range starts building (15 min for long options, 60 min for spreads) |
| 9:45 / 10:30 | Breakout entries can start |
| **11:00** | **No new entries.** Profit window opens: any position up ≥10% is closed to lock in the gain |
| **13:00** | **Profit window ends: everything still open is closed**, winner or loser |
| 15:15 | Safety flatten, in case anything is still open (e.g. an exit order failed) |
| 15:30 | Alpaca stops accepting orders on expiring contracts |

The aim is to make the day's money by midday. 0DTE premium decays fastest in the afternoon,
so a long option that hasn't worked by 1 PM rarely recovers. Turn this off with
`profit_window: enabled: false`, or move the times in `config.yaml`. Take-profit and stop-loss
exits still apply at any time of day.

### Rules in both modes
- **Profit window 11:00–13:00 ET**: lock in gains, then close everything by 13:00 (see above).
- **Safety flatten at 15:15 ET** (earlier on half-days). Alpaca rejects orders on expiring
  ETF options after **15:30 ET** and liquidates them at 15:45, so the config can't be set later than 15:25.
- Max trades per day, max open positions, one entry per ETF per day.
- A **daily loss limit** that flattens and stops trading for the day.
- `skip_dates`: sit out FOMC/CPI days.

Every number can be changed in `config.yaml`. **Why two modes?** Academic studies find that retail
0DTE *buyers* lose money on average, while option sellers have done better but take rare, large
losses. See [docs/RESEARCH.md](docs/RESEARCH.md) for the sources, published backtests of both
approaches, and Alpaca's expiration-day rules.

---

## Setup

### 1. Get an Alpaca account and API keys
1. Sign up at <https://alpaca.markets> (free). You get a **paper trading** account automatically.
2. In the dashboard, switch to your **Paper** account and enable **options trading**
   (level 2 to buy calls/puts; level 3 for `credit_spread` mode).
3. Under **API Keys**, generate a key + secret. Copy them somewhere safe; the secret is shown once.

### 2. Install Python 3.10+
- **macOS:** `brew install python` or download it from <https://www.python.org/downloads/>
- **Windows:** download from <https://www.python.org/downloads/> and tick **"Add python.exe to PATH"**
- **Linux:** usually already installed (`python3 --version`)

### 3. Download and set up the bot

```bash
git clone https://github.com/ndboscher/alpaca.git
cd alpaca
```

**macOS / Linux**
```bash
./setup.sh
```

**Windows (PowerShell)**
```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

The setup creates a private virtual environment in `.venv/`, installs dependencies, and creates
two files for you to edit:

- **`.env`**: your API keys. Paste them in:
  ```
  ALPACA_API_KEY=PK...
  ALPACA_SECRET_KEY=...
  ALPACA_PAPER=true
  ```
- **`config.yaml`**: strategy and risk settings (see the comments in the file).

Both files are git-ignored, so your keys never get committed.

### 4. Run it

Use `./run.sh <command>` on macOS/Linux or `.\run.ps1 <command>` on Windows:

| Command | What it does |
|---|---|
| `check` | Verifies your keys, shows equity, options level and market hours |
| `chain SPY` | Prints today's 0DTE SPY chain near the money (bid/ask/spread/delta) |
| `run --dry-run` | Full bot on live market data, but **orders are only simulated** |
| `run` | Trades on your account (paper unless you've switched to live) |
| `run --exit-after-close` | Same, but quits when the session ends (for schedulers) |
| `status` | Today's P&L and open option positions |
| `flatten` | Closes the bot's 0DTE positions at market (asks for confirmation) |

A sensible first day:

```bash
./run.sh check
./run.sh chain SPY
./run.sh run --dry-run     # leave running during market hours, read the log
```

Start the bot before 9:30 ET, or any time during the session. If it's started outside market hours
it waits for the next open. Stop it with **Ctrl+C**. Stopping does **not** close positions, so
use `flatten` if needed.

> Your computer must stay awake and online while the bot runs. For hands-off running, use a
> small always-on machine or cloud VM (see *Running unattended* below).

---

## Files it writes

| Path | Contents |
|---|---|
| `logs/bot.log` | Full log, rotated daily, 30 days kept |
| `logs/trades.csv` | Every buy/sell: time, contract, qty, price, reason |
| `logs/trades-dry-run.csv` | Same, for dry-run mode |
| `data/<mode>/state-YYYY-MM-DD.json` | Today's trade counts, so a restart doesn't re-enter the same setup |

---

## Configuration highlights

```yaml
mode: long_options          # or credit_spread
symbols: [SPY, QQQ, IWM]
skip_dates: [2026-10-28]    # e.g. FOMC days
schedule:
  opening_range_minutes: 15
  no_new_entries_after: "14:30"
  flatten_at: "15:15"       # must be <= 15:25 (Alpaca's 15:30 cutoff)
risk:
  max_position_usd: 500
  max_daily_loss_usd: 500
  take_profit_pct: 50
  stop_loss_pct: 35
```

See `config.example.yaml` for every option with comments. Unknown keys are rejected,
so a typo fails loudly instead of being silently ignored.

**Market data:** free Alpaca accounts use the `iex` stock feed and `indicative` options feed
(the defaults). If you subscribe to Alpaca's paid data plan, set `stock_feed: sip` and
`options_feed: opra` for real-time consolidated quotes, which give better signals and fills.

---

## Going live (real money)

Live trading takes two deliberate steps:

1. In `.env`: use your **live** API keys and set `ALPACA_PAPER=false`
2. In `config.yaml`: set `live_trading_confirmed: true`

If only one of these is set, the bot refuses to start. Before going live:

- Run on paper for several weeks and review `logs/trades.csv`.
- **Pattern Day Trader rules:** every 0DTE round trip is a day trade. Margin accounts under
  $25,000 are limited in how many day trades they can make. Check Alpaca's current PDT policy for your account.
- Lower `max_position_usd`, `max_trades_per_day` and `max_daily_loss_usd` to amounts you're comfortable losing.
- Alpaca rejects new orders on expiring ETF options after 15:30 ET and auto-liquidates them at
  15:45 ET. The bot flattens at 15:15 ET by default to stay ahead of that.
- In `credit_spread` mode, place your first paper spread and check `logs/bot.log` to confirm
  Alpaca accepts the order's credit price convention (see docs/RESEARCH.md §4).

---

## Running unattended

**macOS / Linux (cron):** start at 9:00 ET on weekdays. Adjust for your timezone.
```cron
0 9 * * 1-5  cd /path/to/alpaca && ./run.sh run --exit-after-close >> logs/cron.log 2>&1
```
`--exit-after-close` makes the bot quit after the session ends, so each day starts one fresh copy.
Alternatively, leave a single `./run.sh run` going continuously (for example inside `tmux`);
it sleeps through closed markets.

**Windows:** use Task Scheduler with action `powershell.exe`, arguments
`-ExecutionPolicy Bypass -File C:\path\to\alpaca\run.ps1 run --exit-after-close`, triggered weekdays at 9:00 ET.

---

## Project layout

```
zdte_bot/
  cli.py        command-line entry point (zdte check/run/chain/status/flatten)
  bot.py        main loop: schedule, exits, entries, persistence
  strategy.py   opening range + VWAP breakout signal (pure functions)
  contracts.py  pick the 0DTE contract to buy (long_options)
  spreads.py    pick, size and exit credit spreads (credit_spread)
  risk.py       sizing, daily limits, take-profit / stop / trailing-stop rules
  broker.py     the only file that talks to Alpaca (alpaca-py)
  config.py     loads .env + config.yaml with validation
  journal.py    trade CSV and daily state file
tests/          unit tests + end-to-end loop tests against a fake broker
docs/RESEARCH.md  sources and evidence behind the strategy choices
```

To write your own strategy, change `strategy.py` (it gets a list of 1-minute bars and returns
`CALL`, `PUT` or nothing). Everything else (contract selection, sizing, exits) stays the same.

## Development

```bash
.venv/bin/pytest            # Windows: .\.venv\Scripts\pytest
```

CI runs the tests on every push (see `.github/workflows/tests.yml`).

## Updating

```bash
git pull
./setup.sh      # or .\setup.ps1 — reinstalls deps, keeps your .env and config.yaml
```
New settings added to `config.example.yaml` use built-in defaults until you copy them into
your `config.yaml`.

**Upgrading from 0.1.0:** if your `config.yaml` has `flatten_at: "15:30"`, the bot will refuse to
start. Change it to `"15:15"`, because Alpaca rejects orders on expiring contracts after 15:30.
