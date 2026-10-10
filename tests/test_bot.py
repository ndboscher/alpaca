"""Drive Bot.tick() end to end against a fake broker."""

from datetime import date, datetime, timedelta

from zdte_bot.bot import ET, Bot
from zdte_bot.broker import Account, Clock, FillResult, OptionPosition
from zdte_bot.config import Config
from zdte_bot.models import Bar, OptionCandidate, Side, parse_occ

TODAY = date(2026, 10, 6)
OPEN = datetime(2026, 10, 6, 9, 30, tzinfo=ET)


class FakeBroker:
    def __init__(self):
        self.dry_run = False
        self.now = OPEN + timedelta(minutes=20)
        self.positions: list[OptionPosition] = []
        self.quotes: dict[str, tuple[float, float]] = {}
        self.orders: list[tuple] = []
        self.pnl = 0.0
        closes = [500.0] * 15 + [500.3, 500.8]
        self.bars = [Bar(OPEN + timedelta(minutes=i), c, c + 0.1, c - 0.1, c, 1000)
                     for i, c in enumerate(closes)]

    def clock(self):
        return Clock(self.now, True, self.now + timedelta(days=1),
                     datetime(2026, 10, 6, 16, 0, tzinfo=ET))

    def account(self):
        return Account(100_000 + self.pnl, 100_000, 50_000, 3, "ACTIVE", False)

    def option_positions(self):
        return list(self.positions)

    def open_order_symbols(self):
        return set()

    def minute_bars(self, symbols, start):
        return {s: self.bars if s == "SPY" else [] for s in symbols}

    def option_chain(self, underlying, expiration, side, spot, width):
        return [OptionCandidate(f"SPY261006C00{k}000", "SPY", TODAY, Side.CALL, float(k),
                                1.00, 1.04, 0.40 if k == 501 else 0.2) for k in (501, 503)]

    def option_quotes(self, symbols):
        return {s: self.quotes[s] for s in symbols if s in self.quotes}

    def buy_limit(self, symbol, qty, limit):
        self.orders.append(("BUY", symbol, qty, limit))
        occ = parse_occ(symbol)
        self.positions.append(OptionPosition(symbol, occ.underlying, occ.expiration, occ.side,
                                             qty, limit, limit))
        return FillResult(qty, limit, "1")

    def sell(self, symbol, qty, limit):
        self.orders.append(("SELL", symbol, qty, limit))
        self.positions = [p for p in self.positions if p.symbol != symbol]
        return FillResult(qty, limit, "2")

    def cancel_orders_for(self, symbols):
        pass


def make_bot(tmp_path, broker):
    cfg = Config(api_key="k", secret_key="s")
    return Bot(cfg, broker, data_dir=tmp_path / "data", log_dir=tmp_path / "logs")


def test_enters_on_breakout_then_takes_profit(tmp_path):
    broker = FakeBroker()
    bot = make_bot(tmp_path, broker)

    bot.tick()
    assert broker.orders[0][0] == "BUY"
    _, sym, qty, limit = broker.orders[0]
    assert sym == "SPY261006C00501000" and qty == 4 and abs(limit - 1.025) < 1e-9

    # Same breakout again: no second entry for SPY today.
    bot.tick()
    assert len(broker.orders) == 1

    broker.quotes[sym] = (1.60, 1.62)
    bot.tick()
    assert broker.orders[-1][:3] == ("SELL", sym, 4)
    assert (tmp_path / "logs" / "trades.csv").read_text().count("\n") == 3


def test_flattens_at_end_of_day(tmp_path):
    broker = FakeBroker()
    bot = make_bot(tmp_path, broker)
    bot.tick()
    broker.now = datetime(2026, 10, 6, 15, 31, tzinfo=ET)
    broker.quotes["SPY261006C00501000"] = (1.0, 1.02)
    bot.tick()
    assert broker.orders[-1][0] == "SELL" and not broker.positions


def test_no_entries_after_cutoff_or_loss_limit(tmp_path):
    broker = FakeBroker()
    broker.now = datetime(2026, 10, 6, 14, 45, tzinfo=ET)
    make_bot(tmp_path, broker).tick()
    assert broker.orders == []

    broker = FakeBroker()
    broker.pnl = -600
    make_bot(tmp_path / "b", broker).tick()
    assert broker.orders == []


def test_state_survives_restart(tmp_path):
    broker = FakeBroker()
    make_bot(tmp_path, broker).tick()
    broker.positions.clear()  # position closed elsewhere
    make_bot(tmp_path, broker).tick()  # fresh bot, same day
    assert len(broker.orders) == 1


def test_run_exits_after_close(tmp_path, monkeypatch):
    broker = FakeBroker()
    bot = make_bot(tmp_path, broker)
    ticks = []

    def fake_sleep(_):
        ticks.append(1)
        broker.now = datetime(2026, 10, 6, 16, 1, tzinfo=ET)
        broker.clock = lambda: Clock(broker.now, False, broker.now + timedelta(hours=17),
                                     broker.now + timedelta(hours=24))

    monkeypatch.setattr("zdte_bot.bot._time.sleep", fake_sleep)
    bot.run(exit_after_close=True)
    assert len(ticks) == 1


def test_profit_window_locks_gain_then_closes_rest(tmp_path):
    broker = FakeBroker()
    bot = make_bot(tmp_path, broker)
    bot.tick()                                    # 09:50 entry at 1.025
    sym = broker.orders[0][1]
    broker.quotes[sym] = (1.14, 1.16)             # +12%: below take-profit, above window min
    broker.now = datetime(2026, 10, 6, 10, 55, tzinfo=ET)
    bot.tick()
    assert len(broker.orders) == 1                # before 11:00 it is left to run
    broker.now = datetime(2026, 10, 6, 11, 1, tzinfo=ET)
    bot.tick()
    assert broker.orders[-1][:2] == ("SELL", sym)


def test_profit_window_time_stop_and_no_late_entries(tmp_path):
    broker = FakeBroker()
    bot = make_bot(tmp_path, broker)
    bot.tick()
    sym = broker.orders[0][1]
    broker.quotes[sym] = (0.90, 0.92)             # -11%: no stop yet
    broker.now = datetime(2026, 10, 6, 12, 0, tzinfo=ET)
    bot.tick()
    assert len(broker.orders) == 1
    broker.now = datetime(2026, 10, 6, 13, 0, tzinfo=ET)
    bot.tick()
    assert broker.orders[-1][:2] == ("SELL", sym)

    late = FakeBroker()
    late.now = datetime(2026, 10, 6, 11, 5, tzinfo=ET)
    make_bot(tmp_path / "late", late).tick()
    assert late.orders == []                      # entries stop at window start
