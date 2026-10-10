# Research notes: 0DTE ETF bots on Alpaca

Notes collected while building zdte-bot, plus the changes each finding led to. Figures
are as reported by their sources and were **not independently verified**. Most backtests
below come from vendors selling the tool that produced them.

_Last updated: 2026-10-10_

---

## 1. Alpaca expiration-day rules (most important)

| Rule | Broad-based ETFs (SPY, QQQ, IWM…) | Single stocks / other options |
|---|---|---|
| Last time to submit orders on expiring contracts | **3:30 PM ET** | 3:15 PM ET |
| Alpaca auto-liquidates expiring positions | **3:45 PM ET** | 3:30 PM ET |

Orders sent after the cutoff are **rejected**. An older Alpaca forum thread quotes earlier,
stricter times (3:00 / 3:15 PM), so these limits have changed before and may change again.

**What changed in the bot:** the default `flatten_at` was 15:30, so its closing orders
would have been rejected. It is now **15:15**, and config validation refuses anything
later than 15:25. If the bot finds positions still open after 15:30, it logs an error
saying Alpaca will liquidate them.

Sources: [Alpaca support: 0DTE cutoff times](https://alpaca.markets/support/what-are-the-cutoff-times-for-trading-0dte-options-on-alpaca),
[Alpaca: How to trade 0DTE options](https://alpaca.markets/learn/how-to-trade-0dte-options-on-alpaca),
[Alpaca forum: Trading 0DTE options](https://forum.alpaca.markets/t/trading-0dte-options/14626)

---

## 2. Is buying 0DTE options a good idea? (academic evidence)

- **Bogousslavsky & Muravyev, "An Anatomy of Retail Option Trading":** retail option
  losses are concentrated in 0DTE contracts. Option *purchases* lose about 3.95% on average,
  while naked option *sales* (13% of trades) earn about 20%. The 0DTE effect gets weaker after
  controls are added.
- **Beckmeyer, Branger & Gayda, "Retail Traders Love 0DTE Options… But Should They?":**
  over 75% of retail S&P 500 option trades are 0DTE, and retail traders lose substantially
  even with the price improvement they get on fills.
- Both studies use 2021–2022 data, not the latest years.

**Takeaway:** buying 0DTE calls and puts (the original `long_options` mode) fights the
average. Selling premium has had better average returns but carries fat-tail risk: rare,
large losses. So the bot now offers a defined-risk selling mode (section 3) and caps the
maximum loss of every spread.

Sources: [Bogousslavsky & Muravyev (PDF)](https://www.lsu.edu/business/files/event-files/2025-finance-mardi-gras/retail_option_trading_v2.pdf),
[Beckmeyer et al. (SSRN)](https://papers.ssrn.com/sol3/Delivery.cfm/4404704.pdf?abstractid=4404704&mirid=1)

---

## 3. The most-documented 0DTE bot: 60-minute opening range breakout, credit spreads

The best-documented retail 0DTE automation is Option Alpha's SPX "ORB 60m" bot:

- Build the **first 60 minutes'** high/low range. Skip the day if the range is **< 0.2%** wide.
- Breakout **above** the range → **sell a put spread** with the short strike at the
  **range low**. Breakout **below** the range → **sell a call spread** with the short strike at the
  **range high**. The wings are $15 wide on SPX.
- No new entries after **12:00 PM ET**. It skips FOMC days and holds to expiration.

Reported results (note the different periods and authors):

| Source | Period | Trades | Win rate | Profit factor | Notes |
|---|---|---|---|---|---|
| Option Alpha (vendor) | not stated | n/a | 89.4% | 1.44 | max drawdown −$3,453 |
| Option Alpha community | n/a | n/a | 88.8% | n/a | avg win $155 vs avg loss **$818** |
| Quantish (3rd party) | May 2022 – Aug 2025 | 661 | 87.0% | 1.3 | 0.2% min-range filter |

The high win rate comes paired with losses about 5× larger than wins, so a few bad days decide
the result. This is why the bot adds a **stop at 100% of credit** and an **exit if the underlying
breaks the short strike**, rather than holding to expiry. On Alpaca it can't hold to expiry anyway
(see section 1).

A different SPY ORB design (Options Cafe, Feb 2024 – Mar 2026) reported a 41% win rate with a
1.99× payoff ratio. That is a debit-style profile closer to `long_options` mode.

**What changed in the bot:** a new `mode: credit_spread` implements this structure with
SPY-scaled defaults: 60-min range, 0.2% minimum range, $2 wings, entries until 12:00, 50%
profit target, 100%-of-credit stop, and a short-strike breach exit. A new `skip_dates` list
lets you sit out FOMC and CPI days.

Sources: [Option Alpha: ORB 0DTE explained](https://optionalpha.com/blog/opening-range-breakout-0dte-options-trading-strategy-explained),
[Option Alpha community: ORB 60m](https://app.optionalpha.com/community/all/post/opening-range-breakout-60m),
[Quantish: SPX opening range breakouts](https://blog.quantish.io/2025/09/09/0dte-spx-opening-range-breakouts/),
[Options Cafe: 0DTE ORB backtest](https://options.cafe/blog/0dte-opening-range-breakout-strategy-spy-backtested-results/),
[Option Alpha: top 0DTE strategies](https://optionalpha.com/learn/top-0dte-options-strategies)

---

## 4. Alpaca multi-leg (spread) orders

- Send them as one order with `order_class="mleg"` and 2–4 `legs`. Each leg has a symbol,
  `ratio_qty`, side and `position_intent` (`sell_to_open`, `buy_to_close`…). Spreads need
  **options level 3**.
- Leg ratios must be in lowest terms: put the multiplier in the top-level `qty` (the bot
  always uses ratio 1:1).
- **Price sign:** Alpaca's own MCP server documents the net `limit_price` as positive for a
  debit and **negative for a credit**, which is what the bot sends. Alpaca's Level 3 docs
  example doesn't state the convention explicitly. **Confirm it on paper** with your first spread
  (`logs/bot.log` shows any rejection).

Sources: [Alpaca: Options Level 3 trading](https://docs.alpaca.markets/us/docs/options-level-3-trading),
[alpaca-py request reference](https://alpaca.markets/sdks/python/api_reference/trading/requests.html),
[alpaca-mcp-server place_option_order](https://glama.ai/mcp/servers/alpacahq/alpaca-mcp-server/tools/place_option_order),
[QuantConnect Lean Alpaca mleg PR](https://github.com/QuantConnect/Lean.Brokerages.Alpaca/pull/79)

---

## 5. Existing open-source bots

No mature, maintained open-source **live** 0DTE bot built on alpaca-py turned up. The closest
are:

- **Alpaca's own tutorial** backtests a 0DTE bull put spread in Python, using alpaca-py for stock
  data and Databento for OPRA option prices. It's a good starting point for a backtester.
- **Hackathon projects** (e.g. OptionForge, and an Alpaca bot with "12 deterministic risk
  gates"). Their useful idea is independent pre-trade risk checks, which this bot already does
  in `risk.py`.
- The GitHub [`0dte` topic](https://github.com/topics/0dte) lists scanners and alert bots, mostly not Alpaca-based.

Sources: [Alpaca: backtesting a 0DTE bull put spread](https://alpaca.markets/learn/backtesting-zero-dte-bull-put-spread-options-strategy-with-python),
[OptionForge](https://lablab.ai/submissions/ium5b9ylmvuuyx3uyc70lwbd)

---

## 6. Midday profit window (owner's requirement)

The bot should make its money by late morning or early afternoon and be done well before
the close. Implemented as `profit_window`: no entries after 11:00 ET, close anything up ≥10%
from 11:00, and close everything at 13:00 ET. The 15:15 flatten stays as a backstop.
Reasoning: 0DTE theta (time decay) accelerates into the afternoon, which works against
long options. For credit spreads it gives up some afternoon decay in exchange for less
exposure to late-day reversals. Whether it helps is a backtest question (roadmap item 1).

## Roadmap suggested by this research

1. **Backtester:** replay historical 1-minute bars plus option quotes (Alpaca historical
   option data or Databento OPRA) through the same `strategy.py` / `spreads.py` code.
   Neither mode should go live without one.
2. **Event calendar:** fill `skip_dates` automatically from the FOMC / CPI schedule.
3. **Iron condor mode:** vendor data (Option Alpha, 180 days, in-sample) suggests ~0.3%-OTM
   condors opened around 1 PM reached max profit about 66% of the time. This is weak evidence; backtest first.
4. **Alerts:** Discord/Telegram notifications on entries, exits and errors.
