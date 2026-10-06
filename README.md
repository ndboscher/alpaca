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

**Strategy: opening range breakout (ORB), buying calls or puts**

1. From 9:30 ET it records the high and low of each ETF for the first 15 minutes (the *opening range*).
2. When a 1-minute bar closes **above the range high** (and above VWAP) it buys a **call**;
   **below the range low** (and below VWAP) it buys a **put**.
3. It picks the 0DTE contract nearest **0.40 delta** that has a tight bid/ask spread and a
   reasonable premium, then sizes the trade to your `max_position_usd`.
4. Every loop (15s) it manages open positions:
   - **take profit** at +50%
   - **stop loss** at −35%
   - **trailing stop**: once up 30%, exit if it falls 20% from its high
   - **flatten everything at 15:30 ET** (earlier on half-days). It never holds through expiry.
5. Risk guards: max trades/day, max open positions, one entry per ETF per day, and a
   **daily loss limit** that flattens and stops trading for the day.

Every number above can be changed in `config.yaml`.

---

## Setup

### 1. Get an Alpaca account and API keys
1. Sign up at <https://alpaca.markets> (free). You get a **paper trading** account automatically.
2. In the dashboard, switch to your **Paper** account and enable **options trading**
   (level 2 or higher is needed to buy calls/puts).
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
symbols: [SPY, QQQ, IWM]
schedule:
  opening_range_minutes: 15
  no_new_entries_after: "14:30"
  flatten_at: "15:30"
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
- Alpaca may close expiring options positions itself late in the day. The bot flattens
  at 15:30 ET by default to stay ahead of that.

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
  contracts.py  pick the 0DTE contract to buy
  risk.py       sizing, daily limits, take-profit / stop / trailing-stop rules
  broker.py     the only file that talks to Alpaca (alpaca-py)
  config.py     loads .env + config.yaml with validation
  journal.py    trade CSV and daily state file
tests/          unit tests + end-to-end loop tests against a fake broker
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
