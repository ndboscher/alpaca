from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from zdte_bot.config import ConfigError, ContractsCfg, RiskCfg, StrategyCfg, _parse_time, load_config
from zdte_bot.contracts import select_contract
from zdte_bot.models import Bar, OptionCandidate, Side, parse_occ
from zdte_bot.risk import DayStats, entry_block_reason, exit_reason, position_size
from zdte_bot.strategy import breakout_signal, opening_range, vwap

ET = ZoneInfo("America/New_York")
OPEN = datetime(2026, 10, 6, 9, 30, tzinfo=ET)


def bars_from(closes, start=OPEN, spread=0.05, volume=1000):
    return [Bar(start + timedelta(minutes=i), c, c + spread, c - spread, c, volume)
            for i, c in enumerate(closes)]


# ---- models ---------------------------------------------------------------

def test_parse_occ():
    o = parse_occ("SPY261006C00570500")
    assert o.underlying == "SPY" and o.side == Side.CALL
    assert o.expiration == date(2026, 10, 6) and o.strike == 570.5
    assert parse_occ("SPY") is None


# ---- strategy -------------------------------------------------------------

def test_opening_range_waits_for_window():
    bars = bars_from([100.0] * 10)
    assert opening_range(bars, OPEN, 15) is None
    bars = bars_from([100.0] * 16)
    rng = opening_range(bars, OPEN, 15)
    assert rng.high == pytest.approx(100.05) and rng.low == pytest.approx(99.95)


def test_vwap():
    assert vwap([]) is None
    assert vwap(bars_from([100.0, 102.0])) == pytest.approx(101.0)


def test_breakout_call_and_put():
    cfg = StrategyCfg(breakout_buffer_pct=0.05, require_vwap_confirm=True, max_range_pct=1.0)
    up = bars_from([100.0] * 15 + [100.1, 100.4])
    sig = breakout_signal(up, OPEN, 15, cfg)
    assert sig is not None and sig.side == Side.CALL

    down = bars_from([100.0] * 15 + [99.9, 99.6])
    sig = breakout_signal(down, OPEN, 15, cfg)
    assert sig is not None and sig.side == Side.PUT

    inside = bars_from([100.0] * 15 + [100.02])
    assert breakout_signal(inside, OPEN, 15, cfg) is None


def test_breakout_skips_wide_range():
    cfg = StrategyCfg(max_range_pct=0.5)
    bars = bars_from([100.0, 101.0, 99.0] + [100.0] * 12 + [102.0])
    assert breakout_signal(bars, OPEN, 15, cfg) is None


def test_breakout_vwap_filter():
    # Heavy volume traded at 101 after the range pushes VWAP above a later 100.3 close.
    bars = bars_from([100.0] * 15)
    bars.append(Bar(OPEN + timedelta(minutes=15), 101, 101.05, 100.95, 101, 1_000_000))
    bars.append(Bar(OPEN + timedelta(minutes=16), 100.3, 100.35, 100.25, 100.3, 1000))
    assert breakout_signal(bars, OPEN, 15, StrategyCfg(require_vwap_confirm=True)) is None
    sig = breakout_signal(bars, OPEN, 15, StrategyCfg(require_vwap_confirm=False))
    assert sig.side == Side.CALL


# ---- contracts ------------------------------------------------------------

def cand(strike, bid, ask, side=Side.CALL, delta=None):
    return OptionCandidate(f"SPY261006{'C' if side == Side.CALL else 'P'}{int(strike * 1000):08d}",
                           "SPY", date(2026, 10, 6), side, strike, bid, ask, delta)


def test_select_by_delta():
    chain = [cand(499, 2.0, 2.1, delta=0.6), cand(500, 1.5, 1.6, delta=0.5),
             cand(501, 1.0, 1.05, delta=0.41), cand(502, 0.6, 0.65, delta=0.3)]
    assert select_contract(chain, Side.CALL, 500.2, ContractsCfg()).strike == 501


def test_select_filters_spread_and_premium():
    cfg = ContractsCfg(max_spread_pct=10, min_premium=0.3, max_premium=5)
    chain = [cand(501, 1.0, 1.5, delta=0.4),     # spread too wide
             cand(502, 0.1, 0.12, delta=0.3),    # too cheap
             cand(503, 0.5, 0.52, delta=0.25)]
    assert select_contract(chain, Side.CALL, 500, cfg).strike == 503


def test_select_without_greeks_prefers_first_otm():
    chain = [cand(499, 0.6, 0.62, Side.PUT), cand(500, 0.8, 0.82, Side.PUT),
             cand(501, 1.2, 1.22, Side.PUT)]
    assert select_contract(chain, Side.PUT, 500.4, ContractsCfg()).strike == 500


# ---- risk -----------------------------------------------------------------

def test_position_size():
    cfg = RiskCfg(max_position_usd=500, max_contracts=5)
    assert position_size(1.20, cfg, 10_000) == 4
    assert position_size(0.50, cfg, 10_000) == 5
    assert position_size(6.00, cfg, 10_000) == 0
    assert position_size(1.00, cfg, 150) == 1


def test_entry_block():
    cfg = RiskCfg(max_trades_per_day=2, max_open_positions=1, max_daily_loss_usd=300)
    assert entry_block_reason(DayStats(), cfg) is None
    assert "loss" in entry_block_reason(DayStats(pnl=-300), cfg)
    assert "trades" in entry_block_reason(DayStats(trades=2), cfg)
    assert "positions" in entry_block_reason(DayStats(open_positions=1), cfg)


def test_exit_rules():
    cfg = RiskCfg(take_profit_pct=50, stop_loss_pct=35, trailing_activate_pct=30, trailing_stop_pct=20)
    assert exit_reason(1.0, 1.10, 1.10, cfg) is None
    assert "take profit" in exit_reason(1.0, 1.55, 1.55, cfg)
    assert "stop loss" in exit_reason(1.0, 0.60, 1.0, cfg)
    assert "trailing" in exit_reason(1.0, 1.10, 1.40, cfg)   # 21% off a +40% high
    assert exit_reason(1.0, 1.10, 1.25, cfg) is None         # trail not yet active


# ---- config ---------------------------------------------------------------

def test_parse_time_handles_yaml_sexagesimal():
    assert _parse_time("14:30", "x") == time(14, 30)
    assert _parse_time(870, "x") == time(14, 30)


def test_config_loads_example(tmp_path, monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("ALPACA_API_KEY=abc\nALPACA_SECRET_KEY=def\nALPACA_PAPER=true\n")
    cfg = load_config("config.example.yaml", env)
    assert cfg.symbols == ["SPY", "QQQ", "IWM"] and cfg.paper
    assert cfg.schedule.flatten_at == time(15, 15)
    assert cfg.mode == "long_options" and cfg.credit_spread.opening_range_minutes == 60


def test_config_refuses_unconfirmed_live(tmp_path, monkeypatch):
    for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_PAPER"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("ALPACA_API_KEY=abc\nALPACA_SECRET_KEY=def\nALPACA_PAPER=false\n")
    with pytest.raises(ConfigError, match="real money"):
        load_config("config.example.yaml", env)


def test_config_rejects_unknown_key(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("risk:\n  max_posiiton_usd: 5\n")
    with pytest.raises(ConfigError, match="max_posiiton_usd"):
        load_config(p, tmp_path / "none.env", require_keys=False)


# ---- profit window ----------------------------------------------------------

def test_window_exit_reason():
    from zdte_bot.config import ProfitWindowCfg
    from zdte_bot.risk import window_exit_reason
    cfg = ProfitWindowCfg(start=time(11), end=time(13), min_profit_pct=10)
    assert window_exit_reason(40, time(10, 59), cfg) is None          # before window: let it run
    assert "lock in" in window_exit_reason(12, time(11, 30), cfg)
    assert window_exit_reason(5, time(11, 30), cfg) is None           # not enough profit yet
    assert window_exit_reason(-20, time(12, 59), cfg) is None
    assert "window closed" in window_exit_reason(-20, time(13, 0), cfg)
    cfg.close_all_at_end = False
    assert window_exit_reason(-20, time(13, 30), cfg) is None
    cfg.enabled = False
    assert window_exit_reason(50, time(12), cfg) is None


def test_profit_window_validation(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text('profit_window:\n  start: "13:00"\n  end: "11:00"\n')
    with pytest.raises(ConfigError, match="profit_window"):
        load_config(p, tmp_path / "none.env", require_keys=False)
