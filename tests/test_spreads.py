from datetime import date, datetime, timedelta

import pytest

from zdte_bot.bot import ET, Bot
from zdte_bot.broker import Account, Clock, FillResult, OptionPosition
from zdte_bot.config import Config, ConfigError, ContractsCfg, CreditSpreadCfg, load_config
from zdte_bot.models import Bar, OptionCandidate, Side, parse_occ
from zdte_bot.spreads import select_credit_spread, spread_exit_reason, spread_size
from zdte_bot.strategy import OpeningRange, Signal

TODAY = date(2026, 10, 6)
OPEN = datetime(2026, 10, 6, 9, 30, tzinfo=ET)


def opt(strike, bid, ask, side):
    sym = f"SPY261006{'C' if side == Side.CALL else 'P'}{int(strike * 1000):08d}"
    return OptionCandidate(sym, "SPY", TODAY, side, float(strike), bid, ask)


PUTS = [opt(k, b, b + 0.02, Side.PUT) for k, b in
        [(494, 0.05), (495, 0.08), (496, 0.12), (497, 0.20), (498, 0.35), (499, 0.55), (500, 0.90)]]
CALLS = [opt(k, b, b + 0.02, Side.CALL) for k, b in
         [(500, 0.90), (501, 0.55), (502, 0.35), (503, 0.20), (504, 0.12), (505, 0.08)]]
RANGE = OpeningRange(high=501.6, low=498.4)


def test_bullish_breakout_sells_put_spread_at_range_low():
    sig = Signal(Side.CALL, 502.0, "up", RANGE)
    sp = select_credit_spread(PUTS + CALLS, sig, CreditSpreadCfg(width=2, min_credit=0.1),
                              ContractsCfg(max_spread_pct=50))
    assert sp.side == Side.PUT and sp.short.strike == 498 and sp.long.strike == 496
    assert sp.mid_credit == pytest.approx(0.23)
    assert sp.natural_credit == pytest.approx(0.35 - 0.14)


def test_bearish_breakout_sells_call_spread_at_range_high():
    sig = Signal(Side.PUT, 498.0, "down", RANGE)
    sp = select_credit_spread(PUTS + CALLS, sig, CreditSpreadCfg(width=2, min_credit=0.1),
                              ContractsCfg(max_spread_pct=50))
    assert sp.side == Side.CALL and sp.short.strike == 502 and sp.long.strike == 504


def test_spread_rejected_when_credit_too_small_or_no_wing():
    sig = Signal(Side.CALL, 502.0, "up", RANGE)
    assert select_credit_spread(PUTS, sig, CreditSpreadCfg(width=2, min_credit=0.5),
                                ContractsCfg(max_spread_pct=50)) is None
    assert select_credit_spread(PUTS, sig, CreditSpreadCfg(width=10, min_credit=0.1),
                                ContractsCfg(max_spread_pct=50)) is None


def test_spread_size_caps_max_loss():
    cfg = CreditSpreadCfg(max_risk_usd=500)
    assert spread_size(0.30, 2.0, cfg, 10, 100_000) == 2      # $170 max loss each
    assert spread_size(0.30, 2.0, cfg, 1, 100_000) == 1
    assert spread_size(0.30, 2.0, cfg, 10, 100) == 0


def test_spread_exits():
    cfg = CreditSpreadCfg(profit_target_pct=50, stop_loss_pct=100)
    assert "take profit" in spread_exit_reason(0.40, 0.18, 502, Side.PUT, 498, cfg)
    assert "stop loss" in spread_exit_reason(0.40, 0.80, 502, Side.PUT, 498, cfg)
    assert "breached" in spread_exit_reason(0.40, 0.50, 497.9, Side.PUT, 498, cfg)
    assert "breached" in spread_exit_reason(0.40, 0.50, 502.1, Side.CALL, 502, cfg)
    assert spread_exit_reason(0.40, 0.35, 500, Side.PUT, 498, cfg) is None


# ---- config ---------------------------------------------------------------

def _cfg_file(tmp_path, text):
    p = tmp_path / "c.yaml"
    p.write_text(text)
    return p


def test_flatten_after_alpaca_cutoff_rejected(tmp_path):
    with pytest.raises(ConfigError, match="15:25"):
        load_config(_cfg_file(tmp_path, 'schedule:\n  flatten_at: "15:30"\n'),
                    tmp_path / "x.env", require_keys=False)


def test_skip_dates_and_mode(tmp_path):
    cfg = load_config(_cfg_file(tmp_path, "mode: credit_spread\nskip_dates: [2026-10-28, '2026-11-13']\n"),
                      tmp_path / "x.env", require_keys=False)
    assert cfg.mode == "credit_spread" and cfg.skip_dates == ["2026-10-28", "2026-11-13"]
    with pytest.raises(ConfigError, match="mode"):
        load_config(_cfg_file(tmp_path, "mode: yolo\n"), tmp_path / "x.env", require_keys=False)


# ---- bot loop in credit_spread mode ---------------------------------------

class SpreadBroker:
    def __init__(self):
        self.dry_run = False
        self.now = OPEN + timedelta(minutes=65)
        self.positions: dict[str, int] = {}
        self.quotes = {c.symbol: (c.bid, c.ask) for c in PUTS + CALLS}
        self.orders: list[tuple] = []
        self.spot = 502.0
        # 60-minute range 498.5-501.5, then a breakout to 502
        closes = [500.0] * 60 + [501.8, 502.0]
        self.bars = [Bar(OPEN + timedelta(minutes=i), c, c + 0.1, c - 0.1, c, 1000)
                     for i, c in enumerate(closes)]
        self.bars[0] = Bar(OPEN, 500, 501.5, 498.5, 500, 1000)

    def clock(self):
        return Clock(self.now, True, self.now + timedelta(days=1),
                     datetime(2026, 10, 6, 16, 0, tzinfo=ET))

    def account(self):
        return Account(100_000, 100_000, 50_000, 3, "ACTIVE", False)

    def option_positions(self):
        out = []
        for s, q in self.positions.items():
            o = parse_occ(s)
            out.append(OptionPosition(s, o.underlying, o.expiration, o.side, q, 0.5, 0.5))
        return out

    def open_order_symbols(self):
        return set()

    def minute_bars(self, symbols, start):
        return {s: self.bars if s == "SPY" else [] for s in symbols}

    def last_price(self, symbol):
        return self.spot

    def option_chain(self, underlying, expiration, side, spot, width):
        return [c for c in PUTS + CALLS if c.side == side]

    def option_quotes(self, symbols):
        return {s: self.quotes[s] for s in symbols if s in self.quotes}

    def open_credit_spread(self, short, long, qty, credit):
        self.orders.append(("OPEN", short, long, qty, credit))
        self.positions[short] = -qty
        self.positions[long] = qty
        return FillResult(qty, credit, "1")

    def close_credit_spread(self, short, long, qty, debit):
        self.orders.append(("CLOSE", short, long, qty, debit))
        self.positions.pop(short, None)
        self.positions.pop(long, None)
        return FillResult(qty, debit, "2")

    def close_position(self, symbol):
        self.orders.append(("CLOSE_POS", symbol))
        self.positions.pop(symbol, None)

    def cancel_orders_for(self, symbols):
        pass


def spread_bot(tmp_path, broker, **overrides):
    cfg = Config(api_key="k", secret_key="s", mode="credit_spread", symbols=["SPY"])
    cfg.contracts.max_spread_pct = 50
    cfg.credit_spread.min_credit = 0.10
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return Bot(cfg, broker, data_dir=tmp_path / "data", log_dir=tmp_path / "logs")


def test_spread_mode_enters_then_takes_profit(tmp_path):
    broker = SpreadBroker()
    bot = spread_bot(tmp_path, broker)
    bot.tick()
    kind, short, long, qty, credit = broker.orders[0]
    assert kind == "OPEN" and short.endswith("P00498000") and long.endswith("P00496000")
    assert qty == 2 and 0.20 <= credit <= 0.23

    broker.quotes[short] = (0.06, 0.08)
    broker.quotes[long] = (0.0, 0.02)
    bot.tick()
    assert broker.orders[-1][0] == "CLOSE" and not broker.positions
    assert not bot.state.spreads


def test_spread_mode_exits_on_breach_and_flattens(tmp_path):
    broker = SpreadBroker()
    bot = spread_bot(tmp_path, broker)
    bot.tick()
    broker.spot = 497.5
    bot.tick()
    assert broker.orders[-1][0] == "CLOSE"

    broker = SpreadBroker()
    bot = spread_bot(tmp_path / "b", broker)
    bot.tick()
    broker.positions["SPY261006C00505000"] = 1   # a stray leg the bot doesn't track
    broker.now = datetime(2026, 10, 6, 15, 16, tzinfo=ET)
    bot.tick()
    kinds = [o[0] for o in broker.orders]
    assert kinds == ["OPEN", "CLOSE", "CLOSE_POS"] and not broker.positions


def test_spread_mode_forgets_spread_closed_by_hand(tmp_path):
    broker = SpreadBroker()
    bot = spread_bot(tmp_path, broker)
    bot.tick()
    broker.positions.clear()
    bot.tick()
    assert not bot.state.spreads and len(broker.orders) == 1


def test_skip_dates_blocks_entries(tmp_path):
    broker = SpreadBroker()
    spread_bot(tmp_path, broker, skip_dates=["2026-10-06"]).tick()
    assert broker.orders == []


def test_no_spread_entries_after_noon(tmp_path):
    broker = SpreadBroker()
    broker.now = datetime(2026, 10, 6, 12, 5, tzinfo=ET)
    spread_bot(tmp_path, broker).tick()
    assert broker.orders == []


def test_spread_mode_closes_orphaned_leg(tmp_path):
    broker = SpreadBroker()
    bot = spread_bot(tmp_path, broker)
    bot.tick()
    long_leg = broker.orders[0][2]
    del broker.positions[long_leg]          # long leg sold by hand -> naked short left
    bot.tick()
    assert broker.orders[-1] == ("CLOSE_POS", broker.orders[0][1])
    assert not bot.state.spreads and not broker.positions


def test_spread_mode_profit_window(tmp_path):
    broker = SpreadBroker()
    bot = spread_bot(tmp_path, broker)
    bot.tick()                                    # 10:35 entry, ~0.22 credit
    _, short, long, _, _ = broker.orders[0]
    broker.quotes[short] = (0.24, 0.26)           # cost to close ~0.17: ~25% of credit kept
    broker.quotes[long] = (0.08, 0.10)
    bot.tick()
    assert len(broker.orders) == 1                # before 11:00, aim for the full 50% target
    broker.now = datetime(2026, 10, 6, 11, 0, tzinfo=ET)
    bot.tick()
    assert broker.orders[-1][0] == "CLOSE" and not bot.state.spreads
