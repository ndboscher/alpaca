"""Live terminal dashboard: `zdte dashboard` (watch only) or `zdte run --dashboard`.

The dashboard never places or cancels orders. It reads the account, positions and market data
from Alpaca, plus the bot's own files (day state, trade journal, log), and redraws once a second.
Data is split from drawing so each part can be tested on its own.
"""

from __future__ import annotations

import csv
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .bot import ET, Bot, DaySchedule, day_schedule
from .broker import Account, Broker, Clock, OptionPosition
from .config import Config
from .journal import DayState
from .models import Bar, parse_occ
from .strategy import breakout_signal, opening_range, vwap

LOG_LINES = 8
TRADE_ROWS = 8


# ----------------------------------------------------------------------------- data

@dataclass
class Snapshot:
    """Everything the dashboard shows, fetched in one go."""
    taken_at: datetime
    clock: Clock | None = None
    account: Account | None = None
    state: DayState | None = None
    bars: dict[str, list[Bar]] = field(default_factory=dict)
    positions: list[OptionPosition] = field(default_factory=list)
    quotes: dict[str, tuple[float, float]] = field(default_factory=dict)
    trades: list[dict] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)
    log_age: float | None = None  # seconds since the bot last wrote to its log
    errors: list[str] = field(default_factory=list)


@dataclass
class Sources:
    """Where the bot keeps its files for this account mode."""
    state_dir: Path
    journal: Path
    log: Path
    dry_run: bool

    @classmethod
    def for_bot(cls, cfg: Config, broker: Broker, log_dir: Path = Path("logs")) -> "Sources":
        bot = Bot(cfg, broker, log_dir=log_dir)  # cheap: just resolves the same paths the bot uses
        return cls(bot.state_dir, bot.journal.path, log_dir / "bot.log", broker.dry_run)


def _tail(path: Path, n: int) -> list[str]:
    if not path.exists():
        return []
    with path.open("rb") as fh:
        fh.seek(0, 2)
        fh.seek(max(0, fh.tell() - 32_000))
        lines = fh.read().decode("utf-8", "replace").splitlines()
    return lines[-n:]


def _todays_trades(path: Path, today: date) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as fh:
        return [r for r in csv.DictReader(fh) if r.get("time", "").startswith(today.isoformat())]


def collect(cfg: Config, broker: Broker, src: Sources) -> Snapshot:
    """Fetch a fresh snapshot. Each part fails on its own so one error doesn't blank the screen."""
    snap = Snapshot(taken_at=datetime.now(ET))

    def attempt(label, fn):
        try:
            return fn()
        except Exception as e:  # shown in the footer; the dashboard keeps running
            snap.errors.append(f"{label}: {e}")
            return None

    snap.clock = attempt("clock", broker.clock)
    snap.account = attempt("account", broker.account)
    now = (snap.clock.now if snap.clock else snap.taken_at).astimezone(ET)
    today = now.date()
    snap.state = attempt("state file", lambda: DayState.load(src.state_dir, today))

    if src.dry_run:
        state = snap.state or DayState(day=today.isoformat())
        snap.positions = [OptionPosition(sym, o.underlying, o.expiration, o.side, p["qty"],
                                         p["entry"], None)
                          for sym, p in state.paper_positions.items() if (o := parse_occ(sym))]
    else:
        held = attempt("positions", broker.option_positions) or []
        snap.positions = [p for p in held if p.underlying in cfg.symbols]

    if snap.clock is not None and now.weekday() < 5:
        sched = day_schedule(cfg, today, snap.clock.next_close)
        if now >= sched.session_open:
            snap.bars = attempt("price bars",
                                lambda: broker.minute_bars(cfg.symbols, sched.session_open)) or {}

    legs = {p.symbol for p in snap.positions}
    if snap.state:
        legs |= {s for sp in snap.state.spreads.values() for s in (sp["short"], sp["long"])}
    if legs:
        snap.quotes = attempt("option quotes", lambda: broker.option_quotes(sorted(legs))) or {}

    snap.trades = attempt("journal", lambda: _todays_trades(src.journal, today)) or []
    snap.log_lines = _tail(src.log, LOG_LINES)
    if src.log.exists():
        snap.log_age = _time.time() - src.log.stat().st_mtime
    return snap


# ------------------------------------------------------------------------ logic

def phase(now: datetime, clock: Clock, sched: DaySchedule | None, cfg: Config,
          skip_day: bool) -> tuple[str, str, str | None, datetime | None]:
    """(label, style, next-event label, next-event time) for the header."""
    if not clock.is_open or sched is None:
        return "MARKET CLOSED", "dim", "opens", clock.next_open.astimezone(ET)
    pw = cfg.profit_window
    if now >= sched.flatten_at:
        return "FLAT FOR THE DAY", "dim", "market closes", sched.close
    if sched.window_end and pw.close_all_at_end and now >= sched.window_end:
        return "DONE FOR THE DAY (profit window over)", "dim", "safety flatten", sched.flatten_at
    if sched.window_start and now >= sched.window_start:
        nxt = ("close everything", sched.window_end) if pw.close_all_at_end else \
              ("safety flatten", sched.flatten_at)
        return "PROFIT WINDOW: locking in gains", "magenta", *nxt
    if skip_day:
        return "SKIP DAY: no new trades", "yellow", "safety flatten", sched.flatten_at
    if now < sched.range_end:
        return "BUILDING OPENING RANGE", "cyan", "entries open", sched.range_end
    if now < sched.last_entry:
        return "WATCHING FOR BREAKOUTS", "green", "entries stop", sched.last_entry
    nxt = ("profit window", sched.window_start) if sched.window_start and now < sched.window_start \
        else ("safety flatten", sched.flatten_at)
    return "MANAGING OPEN TRADES", "yellow", *nxt


def symbol_status(sym: str, bars: list[Bar], now: datetime, sched: DaySchedule, cfg: Config,
                  state: DayState | None, held: set[str]) -> tuple[str, str]:
    """What the bot thinks about one ETF right now, in words."""
    session = [b for b in bars if b.ts >= sched.session_open]
    if not session:
        return "waiting for data", "dim"
    spread_mode = cfg.mode == "credit_spread"
    minutes = int((sched.range_end - sched.session_open).total_seconds() // 60)
    rng = opening_range(session, sched.session_open, minutes)
    if rng is None:
        left = max(0, int((sched.range_end - now).total_seconds() // 60))
        return f"building range ({left}m left)", "cyan"
    if sym in held:
        return "in a trade", "bold green"
    entries = (state.entries_per_symbol.get(sym, 0) if state else 0)
    if entries >= cfg.strategy.max_entries_per_symbol:
        return "traded today", "dim"
    lo_pct = cfg.credit_spread.min_range_pct if spread_mode else 0.0
    hi_pct = cfg.credit_spread.max_range_pct if spread_mode else cfg.strategy.max_range_pct
    if rng.width_pct > hi_pct:
        return f"range {rng.width_pct:.2f}% too wide, sitting out", "yellow"
    if rng.width_pct < lo_pct:
        return f"range {rng.width_pct:.2f}% too narrow, sitting out", "yellow"
    if now >= sched.last_entry:
        return "entries closed", "dim"
    sig = breakout_signal(session, sched.session_open, minutes, cfg.strategy, lo_pct, hi_pct)
    if sig is not None:
        up = sig.side.value == "call"
        action = ("sell put spread" if up else "sell call spread") if spread_mode else \
                 ("buy call" if up else "buy put")
        return f"BREAKOUT {'▲' if up else '▼'} → {action}", "bold green" if up else "bold red"
    last = session[-1].close
    if last > rng.high:
        return "above range, not confirmed yet", "yellow"
    if last < rng.low:
        return "below range, not confirmed yet", "yellow"
    to_hi = (rng.high - last) / last * 100
    to_lo = (last - rng.low) / last * 100
    return f"inside range ({to_hi:.2f}% to high, {to_lo:.2f}% to low)", "white"


def _contract_label(symbol: str) -> str:
    o = parse_occ(symbol)
    return f"{o.underlying} {o.strike:g}{o.side.value[0].upper()}" if o else symbol


def _mid(q: tuple[float, float] | None) -> float | None:
    if not q or q[0] <= 0 or q[1] <= 0:
        return None
    return (q[0] + q[1]) / 2


def _money(v: float) -> Text:
    style = "green" if v > 0 else "red" if v < 0 else "white"
    return Text(f"{'+' if v > 0 else '-' if v < 0 else ''}${abs(v):,.2f}", style=style)


def _pct(v: float) -> Text:
    return Text(f"{v:+.1f}%", style="green" if v > 0 else "red" if v < 0 else "white")


def _ago(seconds: float) -> str:
    s = int(seconds)
    return f"{s}s" if s < 90 else f"{s // 60}m" if s < 5400 else f"{s // 3600}h"


def _until(delta_s: float) -> str:
    s = max(0, int(delta_s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {sec:02d}s"


# ------------------------------------------------------------------------ drawing

def _header(snap: Snapshot, cfg: Config, now: datetime, sched: DaySchedule | None,
            mode_label: str, bot_alive: bool | None) -> Panel:
    skip = now.date().isoformat() in cfg.skip_dates
    if snap.clock is None:
        label, style, nxt, at = "NO CONNECTION", "bold red", None, None
    else:
        label, style, nxt, at = phase(now, snap.clock, sched, cfg, skip)
    mode_style = "bold white on red" if mode_label == "LIVE MONEY" else "bold black on cyan"
    top = Table.grid(expand=True)
    top.add_column()
    top.add_column(justify="right")
    top.add_row(
        Text.assemble((" zdte ", "bold white on blue"), " ", (f" {mode_label} ", mode_style),
                      f"  {cfg.mode}  ·  {' '.join(cfg.symbols)}"),
        Text(now.strftime("%a %b %d  %H:%M:%S ET"), style="bold"))
    line = Text.assemble((f"● {label}", f"bold {style}"))
    if nxt and at:
        line.append(f"    {nxt} in {_until((at - now).total_seconds())}"
                    f" ({at.strftime('%H:%M')})", style="dim")
    if bot_alive is False:
        bot = Text("BOT STOPPED", style="bold white on red")
    elif snap.log_age is None:
        bot = Text("bot log not found", style="yellow")
    else:
        stale = snap.clock is not None and snap.clock.is_open and \
            snap.log_age > max(120, cfg.poll_seconds * 6)
        bot = Text(f"bot active {_ago(snap.log_age)} ago",
                   style="bold yellow" if stale else "dim")
        if stale:
            bot.append("  (is it running?)", style="yellow")
    top.add_row(line, bot)
    return Panel(top, box=box.HEAVY, border_style=style.split()[-1] if style != "dim" else "blue")


def _account_panel(snap: Snapshot, cfg: Config) -> Panel:
    a, r = snap.account, cfg.risk
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim")
    t.add_column(justify="right")
    if a is None:
        return Panel(Text("unavailable", style="red"), title="Account", border_style="red")
    t.add_row("Equity", f"${a.equity:,.2f}")
    t.add_row("Today", _money(a.day_pnl))
    used = max(0.0, -a.day_pnl) / r.max_daily_loss_usd if r.max_daily_loss_usd else 0
    bar_w = 16
    filled = min(bar_w, round(used * bar_w))
    color = "green" if used < 0.5 else "yellow" if used < 0.8 else "red"
    t.add_row("Loss limit", Text.assemble(("█" * filled, color), ("░" * (bar_w - filled), "dim"),
                                          f" ${r.max_daily_loss_usd:,.0f}"))
    trades = snap.state.trades if snap.state else 0
    open_n = (len(snap.state.spreads) if snap.state else 0) if cfg.mode == "credit_spread" \
        else sum(1 for p in snap.positions if p.qty > 0)
    t.add_row("Trades", f"{trades} / {r.max_trades_per_day}")
    t.add_row("Open", f"{open_n} / {r.max_open_positions}")
    t.add_row("Options BP", f"${a.options_buying_power:,.0f}")
    if a.trading_blocked:
        t.add_row("", Text("TRADING BLOCKED", style="bold red"))
    return Panel(t, title="Account", border_style="blue")


def _schedule_panel(now: datetime, sched: DaySchedule | None) -> Panel:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim")
    t.add_column()
    if sched is None:
        return Panel(Text("market closed", style="dim"), title="Today", border_style="blue")
    rows = [("Open", sched.session_open), ("Range set", sched.range_end),
            ("Entries stop", sched.last_entry)]
    if sched.window_start:
        rows += [("Profit window", sched.window_start), ("Close all", sched.window_end)]
    rows += [("Safety flatten", sched.flatten_at), ("Close", sched.close)]
    for name, at in sorted(rows, key=lambda r: r[1]):
        done = now >= at
        t.add_row(Text(("✓ " if done else "  ") + name, style="dim" if done else "white"),
                  Text(at.strftime("%H:%M"), style="dim" if done else "bold"))
    return Panel(t, title="Today", border_style="blue")


def _market_table(snap: Snapshot, cfg: Config, now: datetime,
                  sched: DaySchedule | None) -> Panel:
    t = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False, show_edge=False)
    for col, just in (("ETF", "left"), ("Last", "right"), ("Chg", "right"), ("Range low", "right"),
                      ("Range high", "right"), ("VWAP", "right"), ("Bot sees", "left")):
        t.add_column(col, justify=just, no_wrap=col != "Bot sees")
    if cfg.mode == "credit_spread":
        held = {sp["underlying"] for sp in (snap.state.spreads.values() if snap.state else [])}
    else:
        held = {p.underlying for p in snap.positions if p.qty > 0}
    for sym in cfg.symbols:
        bars = snap.bars.get(sym, [])
        if not bars or sched is None:
            t.add_row(sym, "-", "-", "-", "-", "-", Text("no session data", style="dim"))
            continue
        session = [b for b in bars if b.ts >= sched.session_open]
        minutes = int((sched.range_end - sched.session_open).total_seconds() // 60)
        rng = opening_range(session, sched.session_open, minutes) if session else None
        last = bars[-1].close
        chg = (last - session[0].open) / session[0].open * 100 if session else 0.0
        vw = vwap(session)
        status, style = symbol_status(sym, bars, now, sched, cfg, snap.state, held)
        t.add_row(sym, f"{last:.2f}", _pct(chg),
                  f"{rng.low:.2f}" if rng else "…", f"{rng.high:.2f}" if rng else "…",
                  f"{vw:.2f}" if vw else "-", Text(status, style=style))
    return Panel(t, title="Market", border_style="blue")


def _positions_table(snap: Snapshot, cfg: Config) -> Panel:
    t = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False, show_edge=False)
    total = 0.0
    if cfg.mode == "credit_spread":
        for col in ("Spread", "Qty", "Credit", "To close", "P&L", "Kept", "Exits at (cost)"):
            t.add_column(col, justify="left" if col in ("Spread", "Exits at (cost)") else "right")
        cs = cfg.credit_spread
        for key, sp in (snap.state.spreads.items() if snap.state else []):
            short_mid, long_mid = _mid(snap.quotes.get(sp["short"])), _mid(snap.quotes.get(sp["long"]))
            o = parse_occ(sp["short"])
            lo = parse_occ(sp["long"])
            name = f"{sp['underlying']} {o.strike:g}/{lo.strike:g}{o.side.value[0].upper()}" \
                if o and lo else key
            tp = sp["credit"] * (1 - cs.profit_target_pct / 100)
            sl = sp["credit"] * (1 + cs.stop_loss_pct / 100)
            exits = f"≤{tp:.2f} win · ≥{sl:.2f} stop · strike {sp['short_strike']:g}"
            if short_mid is None or long_mid is None:
                t.add_row(name, str(sp["qty"]), f"{sp['credit']:.2f}", "?", "-", "-", exits)
                continue
            cost = max(0.0, short_mid - long_mid)
            pnl = (sp["credit"] - cost) * 100 * sp["qty"]
            total += pnl
            t.add_row(name, str(sp["qty"]), f"{sp['credit']:.2f}", f"{cost:.2f}", _money(pnl),
                      _pct((sp["credit"] - cost) / sp["credit"] * 100), exits)
    else:
        for col in ("Contract", "Qty", "Entry", "Mark", "P&L", "%", "Exits at"):
            t.add_column(col, justify="left" if col in ("Contract", "Exits at") else "right")
        r = cfg.risk
        for p in sorted(snap.positions, key=lambda p: p.symbol):
            mark = _mid(snap.quotes.get(p.symbol)) or p.current_price
            tp, sl = p.avg_entry * (1 + r.take_profit_pct / 100), p.avg_entry * (1 - r.stop_loss_pct / 100)
            exits = f"{tp:.2f} target · {sl:.2f} stop"
            hw = snap.state.high_water.get(p.symbol) if snap.state else None
            if hw and p.avg_entry and (hw - p.avg_entry) / p.avg_entry * 100 >= r.trailing_activate_pct:
                exits += f" · trail {hw * (1 - r.trailing_stop_pct / 100):.2f}"
            if not mark:
                t.add_row(_contract_label(p.symbol), str(p.qty), f"{p.avg_entry:.2f}", "?", "-", "-", exits)
                continue
            pnl = (mark - p.avg_entry) * 100 * p.qty
            total += pnl
            t.add_row(_contract_label(p.symbol), str(p.qty), f"{p.avg_entry:.2f}", f"{mark:.2f}",
                      _money(pnl), _pct((mark - p.avg_entry) / p.avg_entry * 100), exits)
    if not t.row_count:
        return Panel(Text("No open positions", style="dim"), title="Positions", border_style="blue")
    return Panel(t, title=Text.assemble("Positions  ", ("open P&L ", "dim"), _money(total)),
                 border_style="blue")


def _trades_table(snap: Snapshot) -> Panel:
    if not snap.trades:
        return Panel(Text("No trades yet today", style="dim"), title="Today's orders",
                     border_style="blue")
    t = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False, show_edge=False)
    for col, just in (("Time", "left"), ("Action", "left"), ("Contract", "left"),
                      ("Qty", "right"), ("Price", "right"), ("Why", "left")):
        t.add_column(col, justify=just, no_wrap=col != "Why", overflow="ellipsis")
    styles = {"BUY": "green", "OPEN_SPREAD": "green", "SELL": "yellow", "CLOSE_SPREAD": "yellow",
              "CLOSE": "yellow"}
    for row in snap.trades[-TRADE_ROWS:]:
        sym = row.get("symbol", "")
        label = " / ".join(_contract_label(s) for s in sym.split("/"))
        t.add_row(row.get("time", "")[11:19], Text(row.get("action", ""),
                  style=styles.get(row.get("action", ""), "white")), label, row.get("qty", ""),
                  row.get("price", "") or "mkt", row.get("reason", ""))
    return Panel(t, title=f"Today's orders ({len(snap.trades)})", border_style="blue")


def _log_panel(snap: Snapshot, max_lines: int = LOG_LINES) -> Panel:
    text = Text()
    for line in snap.log_lines[-max_lines:] if max_lines > 0 else []:
        style = ("red" if " ERROR " in line or "CRITICAL" in line else
                 "yellow" if " WARNING " in line else
                 "bold" if any(k in line for k in ("ENTRY", "EXIT", "SIGNAL")) else "dim")
        text.append(line[11:] if line[:4].isdigit() else line, style=style)
        text.append("\n")
    if not snap.log_lines:
        text.append("No bot log yet (logs/bot.log)", style="dim")
    text.rstrip()
    return Panel(text, title="Bot log", border_style="blue")


def render(snap: Snapshot, cfg: Config, now: datetime, mode_label: str,
           bot_alive: bool | None = None, refresh_s: float | None = None,
           console: Console | None = None) -> Group:
    """Build the whole screen. With a console, the log panel shrinks to fit its height."""
    sched = None
    if snap.clock is not None and snap.clock.is_open:
        sched = day_schedule(cfg, now.date(), snap.clock.next_close)
    elif snap.clock is not None and now.weekday() < 5 and snap.bars:
        # After the bell: still show today's ranges and timetable.
        sched = day_schedule(cfg, now.date(), datetime.combine(now.date(), time(16), tzinfo=ET))
    row = Table.grid(expand=True, padding=(0, 1))
    row.add_column(ratio=1)
    row.add_column(ratio=1)
    row.add_row(_account_panel(snap, cfg), _schedule_panel(now, sched))
    footer = Text(f"data {_ago((now - snap.taken_at).total_seconds())} old", style="dim")
    if refresh_s:
        footer.append(f" · refresh every {refresh_s:g}s", style="dim")
    footer.append(" · read-only · Ctrl-C to quit", style="dim")
    for err in snap.errors[:2]:
        footer.append(f"\n! {err[:160]}", style="red")
    top = [_header(snap, cfg, now, sched, mode_label, bot_alive), row,
           _market_table(snap, cfg, now, sched), _positions_table(snap, cfg), _trades_table(snap)]
    log_lines = LOG_LINES
    if console is not None:
        used = len(console.render_lines(Group(*top, footer), pad=False))
        log_lines = min(LOG_LINES, console.height - used - 2)  # 2 = log panel border
    parts = top + ([_log_panel(snap, log_lines)] if log_lines > 0 else []) + [footer]
    return Group(*parts)


# ------------------------------------------------------------------------ loop

def run_dashboard(cfg: Config, broker: Broker, refresh: float | None = None,
                  bot_thread: threading.Thread | None = None, stop_with_bot: bool = False,
                  console: Console | None = None) -> None:
    """Redraw every second; fetch new data every `refresh` seconds."""
    src = Sources.for_bot(cfg, broker)
    refresh = refresh or max(5.0, float(cfg.poll_seconds))
    mode_label = "DRY RUN" if broker.dry_run else ("PAPER" if cfg.paper else "LIVE MONEY")
    console = console or Console()
    snap = collect(cfg, broker, src)
    last_fetch = _time.monotonic()
    with Live(console=console, screen=True, auto_refresh=False) as live:
        while True:
            alive = bot_thread.is_alive() if bot_thread is not None else None
            if stop_with_bot and alive is False:
                return
            if _time.monotonic() - last_fetch >= refresh:
                snap = collect(cfg, broker, src)
                last_fetch = _time.monotonic()
            live.update(render(snap, cfg, datetime.now(ET), mode_label, alive, refresh, console),
                        refresh=True)
            _time.sleep(1)
