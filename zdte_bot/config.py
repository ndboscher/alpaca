"""Configuration loading: secrets from .env, settings from config.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, time
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    pass


# Alpaca rejects orders on expiring broad-ETF options (SPY, QQQ, IWM...) after 15:30 ET and
# auto-liquidates them at 15:45 ET. We must be completely flat before the cutoff.
EXPIRY_ORDER_CUTOFF = time(15, 30)
LATEST_FLATTEN = time(15, 25)

MODES = ("long_options", "credit_spread")


@dataclass
class Schedule:
    opening_range_minutes: int = 15
    no_new_entries_after: time = time(14, 30)
    flatten_at: time = time(15, 15)


@dataclass
class ProfitWindowCfg:
    """Make the money by midday: lock in gains from `start`, be flat by `end`.

    0DTE premium decays fastest in the afternoon, so long options that haven't worked by
    early afternoon rarely recover. From `start` any position up at least `min_profit_pct`
    is closed; at `end` everything still open is closed (when `close_all_at_end`).
    New entries stop at `start`. The hard flatten at schedule.flatten_at still applies.
    """
    enabled: bool = True
    start: time = time(11, 0)
    end: time = time(13, 0)
    min_profit_pct: float = 10
    close_all_at_end: bool = True


@dataclass
class StrategyCfg:
    breakout_buffer_pct: float = 0.05
    require_vwap_confirm: bool = True
    max_range_pct: float = 1.0
    max_entries_per_symbol: int = 1


@dataclass
class CreditSpreadCfg:
    """Opening-range breakout that SELLS a vertical spread on the opposite side of the range."""
    opening_range_minutes: int = 60
    min_range_pct: float = 0.2
    max_range_pct: float = 1.5
    no_new_entries_after: time = time(12, 0)
    width: float = 2.0
    min_credit: float = 0.20
    max_risk_usd: float = 500
    profit_target_pct: float = 50
    stop_loss_pct: float = 100
    exit_on_short_strike_breach: bool = True


@dataclass
class ContractsCfg:
    target_delta: float = 0.40
    max_strike_distance_pct: float = 1.5
    min_premium: float = 0.30
    max_premium: float = 5.00
    max_spread_pct: float = 10.0


@dataclass
class RiskCfg:
    max_position_usd: float = 500
    max_contracts: int = 5
    max_open_positions: int = 2
    max_trades_per_day: int = 4
    max_daily_loss_usd: float = 500
    take_profit_pct: float = 50
    stop_loss_pct: float = 35
    trailing_activate_pct: float = 30
    trailing_stop_pct: float = 20


@dataclass
class ExecutionCfg:
    entry_slippage: float = 0.25
    fill_timeout_seconds: int = 20
    use_market_orders_for_exits: bool = True


@dataclass
class Config:
    api_key: str = ""
    secret_key: str = ""
    paper: bool = True
    mode: str = "long_options"
    symbols: list[str] = field(default_factory=lambda: ["SPY", "QQQ", "IWM"])
    skip_dates: list[str] = field(default_factory=list)
    live_trading_confirmed: bool = False
    stock_feed: str = "iex"
    options_feed: str = "indicative"
    poll_seconds: int = 15
    schedule: Schedule = field(default_factory=Schedule)
    profit_window: ProfitWindowCfg = field(default_factory=ProfitWindowCfg)
    strategy: StrategyCfg = field(default_factory=StrategyCfg)
    contracts: ContractsCfg = field(default_factory=ContractsCfg)
    credit_spread: CreditSpreadCfg = field(default_factory=CreditSpreadCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    execution: ExecutionCfg = field(default_factory=ExecutionCfg)


def _parse_time(value: Any, name: str) -> time:
    if isinstance(value, time):
        return value
    if isinstance(value, int):  # YAML 1.1 parses unquoted 14:30 as sexagesimal minutes
        return time(value // 60, value % 60)
    try:
        hh, mm = str(value).split(":")
        return time(int(hh), int(mm))
    except ValueError as e:
        raise ConfigError(f"{name}: expected HH:MM, got {value!r}") from e


def _fill(obj: Any, data: dict[str, Any], prefix: str) -> None:
    """Copy keys from `data` onto dataclass `obj`, recursing into nested sections."""
    known = {f.name: f for f in fields(obj)}
    for key, value in (data or {}).items():
        if key not in known or key in ("api_key", "secret_key", "paper"):
            raise ConfigError(f"Unknown config key: {prefix}{key}")
        current = getattr(obj, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ConfigError(f"{prefix}{key} must be a section")
            _fill(current, value, f"{prefix}{key}.")
        elif isinstance(current, time):
            setattr(obj, key, _parse_time(value, prefix + key))
        elif isinstance(current, bool):
            if not isinstance(value, bool):
                raise ConfigError(f"{prefix}{key} must be true or false")
            setattr(obj, key, value)
        elif isinstance(current, (int, float)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"{prefix}{key} must be a number")
            setattr(obj, key, type(current)(value))
        else:
            setattr(obj, key, value)


def _validate(cfg: Config) -> None:
    if not cfg.symbols:
        raise ConfigError("symbols must list at least one ticker")
    cfg.symbols = [s.upper().strip() for s in cfg.symbols]
    if cfg.mode not in MODES:
        raise ConfigError(f"mode must be one of {', '.join(MODES)}")
    try:  # YAML gives date objects for unquoted 2026-10-28, strings for quoted ones
        cfg.skip_dates = [d.isoformat() if isinstance(d, date) else date.fromisoformat(str(d)).isoformat()
                          for d in (cfg.skip_dates or [])]
    except ValueError as e:
        raise ConfigError(f"skip_dates must be YYYY-MM-DD dates: {e}") from e
    if cfg.schedule.flatten_at > LATEST_FLATTEN:
        raise ConfigError(f"schedule.flatten_at must be {LATEST_FLATTEN:%H:%M} or earlier: Alpaca "
                          f"rejects orders on expiring ETF options after {EXPIRY_ORDER_CUTOFF:%H:%M} ET")
    if cfg.stock_feed not in ("iex", "sip", "delayed_sip"):
        raise ConfigError("stock_feed must be iex, sip or delayed_sip")
    if cfg.options_feed not in ("indicative", "opra"):
        raise ConfigError("options_feed must be indicative or opra")
    if cfg.schedule.flatten_at <= cfg.schedule.no_new_entries_after:
        raise ConfigError("schedule.flatten_at must be after schedule.no_new_entries_after")
    pw = cfg.profit_window
    if pw.enabled and not pw.start < pw.end <= cfg.schedule.flatten_at:
        raise ConfigError("profit_window: need start < end <= schedule.flatten_at")
    if pw.enabled and pw.min_profit_pct < 0:
        raise ConfigError("profit_window.min_profit_pct can't be negative")
    cs = cfg.credit_spread
    if cs.width <= 0 or cs.min_credit <= 0 or cs.min_credit >= cs.width:
        raise ConfigError("credit_spread: need 0 < min_credit < width")
    if cs.no_new_entries_after >= cfg.schedule.flatten_at:
        raise ConfigError("credit_spread.no_new_entries_after must be before schedule.flatten_at")
    if not 0 < cfg.contracts.target_delta < 1:
        raise ConfigError("contracts.target_delta must be between 0 and 1")
    if not 0 <= cfg.execution.entry_slippage <= 1:
        raise ConfigError("execution.entry_slippage must be between 0 and 1")
    if cfg.risk.stop_loss_pct <= 0 or cfg.risk.take_profit_pct <= 0:
        raise ConfigError("risk.stop_loss_pct and risk.take_profit_pct must be positive")


def load_config(config_path: str | Path = "config.yaml", env_path: str | Path = ".env",
                require_keys: bool = True) -> Config:
    load_dotenv(env_path)
    cfg = Config()

    path = Path(config_path)
    if path.exists():
        with path.open() as fh:
            _fill(cfg, yaml.safe_load(fh) or {}, "")
    elif Path(config_path) != Path("config.yaml"):
        raise ConfigError(f"Config file not found: {config_path}")

    cfg.api_key = os.getenv("ALPACA_API_KEY", "").strip()
    cfg.secret_key = os.getenv("ALPACA_SECRET_KEY", "").strip()
    cfg.paper = os.getenv("ALPACA_PAPER", "true").strip().lower() not in ("false", "0", "no")

    _validate(cfg)

    if require_keys and (not cfg.api_key or not cfg.secret_key or "your_" in cfg.api_key):
        raise ConfigError("Missing Alpaca API keys. Put ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")
    if not cfg.paper and not cfg.live_trading_confirmed:
        raise ConfigError("ALPACA_PAPER=false but live_trading_confirmed is not true in config.yaml. "
                          "Refusing to trade real money.")
    return cfg
