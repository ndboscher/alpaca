"""Terminal dashboard: schedule/phase logic, per-ETF status, data collection and drawing."""

import io
import threading
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from rich.console import Console

from zdte_bot.bot import ET, day_schedule
from zdte_bot.broker import Account, Clock, OptionPosition
from zdte_bot.config import Config
from zdte_bot.dashboard import Snapshot, Sources, collect, phase, render, symbol_status
from zdte_bot.journal import DayState
from zdte_bot.models import Bar, Side

TODAY = date(2026, 10, 6)
OPEN = datetime(2026, 10, 6, 9, 30, tzinfo=ET)
CLOSE = datetime(2026, 10, 6, 16, 0, tzinfo=ET)


def at(h, m):
    return datetime(2026, 10, 6, h, m, tzinfo=ET)


def bars(closes, start=OPEN):
    return [Bar(start + timedelta(minutes=i), c, c + 0.05, c - 0.05, c, 1000)
            for i, c in enumerate(closes)]


def clock(now, is_open=True):
    return Clock(now, is_open, now + timedelta(days=1), CLOSE)


# ---- schedule & phase --------------------------------------------------------

def test_day_schedule_profit_window_caps_entries():
    s = day_schedule(Config(), TODAY, CLOSE)
    assert s.range_end == at(9, 45)
    assert s.last_entry == at(11, 0)          # profit window start beats 14:30
    assert s.flatten_at == at(15, 15)


def test_day_schedule_half_day_flattens_early():
    s = day_schedule(Config(), TODAY, at(13, 0))
    assert s.flatten_at == at(12, 45)
    assert s.last_entry == at(11, 0)


def test_phase_walks_through_the_day():
    cfg = Config()
    s = day_schedule(cfg, TODAY, CLOSE)
    labels = [phase(t, clock(t), s, cfg, False)[0] for t in
              (at(9, 35), at(10, 0), at(11, 30), at(13, 30), at(15, 20))]
    assert labels == ["BUILDING OPENING RANGE", "WATCHING FOR BREAKOUTS",
                      "PROFIT WINDOW: locking in gains", "DONE FOR THE DAY (profit window over)",
                      "FLAT FOR THE DAY"]
    assert phase(at(10, 0), clock(at(10, 0)), s, cfg, False)[2:] == ("entries stop", at(11, 0))
    assert phase(at(8, 0), clock(at(8, 0), is_open=False), None, cfg, False)[0] == "MARKET CLOSED"
    assert phase(at(10, 0), clock(at(10, 0)), s, cfg, True)[0].startswith("SKIP DAY")


# ---- per-ETF status ----------------------------------------------------------

def status(closes, now, cfg=None, state=None, held=()):
    cfg = cfg or Config()
    return symbol_status("SPY", bars(closes), now, day_schedule(cfg, TODAY, CLOSE), cfg,
                         state, set(held))[0]


FLAT = [500.0] * 15


def test_status_building_range():
    assert status([500.0] * 5, at(9, 35)) == "building range (10m left)"


def test_status_breakout_and_inside():
    assert status(FLAT + [500.3, 500.8], at(9, 47)).startswith("BREAKOUT ▲ → buy call")
    assert status(FLAT + [500.0, 500.01], at(9, 47)).startswith("inside range")
    cfg = Config(mode="credit_spread")  # 60-minute range, must be at least 0.2% wide
    swing = [499.0, 500.5] * 30
    assert status(swing + [503.0], at(10, 31), cfg) == "BREAKOUT ▲ → sell put spread"
    assert status([500.0] * 60 + [503.0], at(10, 31), cfg).startswith("range 0.02% too narrow")


def test_status_traded_held_closed_and_wide():
    traded = DayState(day=TODAY.isoformat(), entries_per_symbol={"SPY": 1})
    assert status(FLAT + [500.8], at(9, 47), state=traded) == "traded today"
    assert status(FLAT + [500.8], at(9, 47), held={"SPY"}) == "in a trade"
    assert status(FLAT + [500.0], at(11, 30)) == "entries closed"
    wide = [500.0, 510.0] + [505.0] * 14
    assert "too wide" in status(wide, at(9, 47))


# ---- collect & render --------------------------------------------------------

class FakeBroker:
    dry_run = False

    def __init__(self, now=at(10, 5), fail=()):
        self.now, self.fail = now, set(fail)

    def _maybe_fail(self, name):
        if name in self.fail:
            raise ConnectionError(f"{name} down")

    def clock(self):
        self._maybe_fail("clock")
        return clock(self.now)

    def account(self):
        self._maybe_fail("account")
        return Account(25_300, 25_000, 20_000, 3, "ACTIVE", False)

    def option_positions(self):
        return [OptionPosition("SPY261006C00501000", "SPY", TODAY, Side.CALL, 2, 1.00, 1.20),
                OptionPosition("TSLA261006C00300000", "TSLA", TODAY, Side.CALL, 1, 5.0, 5.0)]

    def minute_bars(self, symbols, start):
        self._maybe_fail("bars")
        return {s: bars(FLAT + [500.4] * 20) for s in symbols}

    def option_quotes(self, symbols):
        return {s: (1.30, 1.34) for s in symbols}


def sources(tmp_path: Path, dry_run=False) -> Sources:
    return Sources(tmp_path / "state", tmp_path / "trades.csv", tmp_path / "bot.log", dry_run)


def screen(snap, cfg, now, **kw) -> str:
    console = Console(width=120, record=True, force_terminal=False)
    console.print(render(snap, cfg, now, "PAPER", **kw))
    return console.export_text()


def test_collect_and_render_long_mode(tmp_path):
    src = sources(tmp_path)
    src.journal.write_text("time,action,symbol,qty,price,reason,dry_run\n"
                           "2026-10-05T09:50:00,BUY,SPY261005C00500000,1,1.00,old,False\n"
                           "2026-10-06T09:47:00,BUY,SPY261006C00501000,2,1.00,breakout,False\n")
    src.log.write_text("2026-10-06 09:47:00 INFO    ENTRY SPY261006C00501000 x2\n")
    snap = collect(Config(), FakeBroker(), src)
    assert snap.errors == []
    assert [p.underlying for p in snap.positions] == ["SPY"]          # other tickers ignored
    assert len(snap.trades) == 1                                      # only today's orders
    out = screen(snap, Config(), at(10, 5))
    for expected in ("WATCHING FOR BREAKOUTS", "SPY 501C", "+$64.00", "+32.0%", "in a trade",
                     "ENTRY SPY261006C00501000", "Today's orders (1)", "+$300.00"):
        assert expected in out, expected


def test_collect_dry_run_uses_simulated_positions(tmp_path):
    src = sources(tmp_path, dry_run=True)
    DayState(day=TODAY.isoformat(), trades=1,
             paper_positions={"QQQ261006P00480000": {"qty": 3, "entry": 2.0}}).save(src.state_dir)
    snap = collect(Config(), FakeBroker(), src)
    assert [(p.symbol, p.qty) for p in snap.positions] == [("QQQ261006P00480000", 3)]


def test_render_credit_spread(tmp_path):
    cfg = Config(mode="credit_spread")
    src = sources(tmp_path)
    DayState(day=TODAY.isoformat(), trades=1, spreads={"S/L": {
        "underlying": "SPY", "side": "put", "short": "SPY261006P00498000",
        "long": "SPY261006P00496000", "short_strike": 498.0, "qty": 2, "credit": 0.40}}
    ).save(src.state_dir)
    broker = FakeBroker(now=at(10, 45))
    broker.option_quotes = lambda syms: {"SPY261006P00498000": (0.20, 0.22),
                                         "SPY261006P00496000": (0.05, 0.07)}
    snap = collect(cfg, broker, src)
    out = screen(snap, cfg, at(10, 45))
    assert "SPY 498/496P" in out and "+$50.00" in out and "≤0.20 win" in out


def test_one_failing_call_does_not_blank_the_screen(tmp_path):
    snap = collect(Config(), FakeBroker(fail={"bars"}), sources(tmp_path))
    assert snap.account is not None and any("price bars" in e for e in snap.errors)
    assert "price bars: bars down" in screen(snap, Config(), at(10, 5))


def test_render_without_connection_or_data():
    out = screen(Snapshot(taken_at=at(10, 0)), Config(), at(10, 0))
    assert "NO CONNECTION" in out and "No open positions" in out


def test_render_market_closed_and_stopped_bot():
    snap = Snapshot(taken_at=at(8, 0), clock=clock(at(8, 0), is_open=False), log_age=30)
    out = screen(snap, Config(), at(8, 0), bot_alive=False)
    assert "MARKET CLOSED" in out and "BOT STOPPED" in out


def test_stale_bot_warning():
    snap = Snapshot(taken_at=at(10, 0), clock=clock(at(10, 0)), log_age=600)
    assert "is it running?" in screen(snap, Config(), at(10, 0))


def test_log_panel_shrinks_to_fit_terminal():
    snap = Snapshot(taken_at=at(10, 0), clock=clock(at(10, 0)),
                    log_lines=[f"2026-10-06 09:{i:02d}:00 INFO    line {i}" for i in range(8)])
    tall = Console(width=120, height=60, record=True)
    tall.print(render(snap, Config(), at(10, 0), "PAPER", console=tall))
    short = Console(width=120, height=34, record=True)
    short.print(render(snap, Config(), at(10, 0), "PAPER", console=short))
    assert "line 0" in tall.export_text()
    assert "line 7" in short.export_text() and "line 0" not in short.export_text()


def test_live_loop_redraws_and_stops_with_bot(tmp_path, monkeypatch):
    from zdte_bot.dashboard import run_dashboard
    monkeypatch.chdir(tmp_path)
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=120, height=40)
    bot = threading.Thread(target=_time.sleep, args=(1.5,))
    bot.start()
    run_dashboard(Config(), FakeBroker(), bot_thread=bot, stop_with_bot=True, console=console)
    assert "zdte" in buf.getvalue() and "Market" in buf.getvalue()


def test_cli_has_dashboard_commands(capsys):
    from zdte_bot.cli import main
    with pytest.raises(SystemExit) as e:
        main(["dashboard", "--help"])
    assert e.value.code == 0 and "--refresh" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["run", "--help"])
    assert "--dashboard" in capsys.readouterr().out
