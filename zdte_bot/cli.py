"""Command line entry point: `zdte <command>`."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from alpaca.common.exceptions import APIError

from . import __version__
from .bot import ET, Bot
from .broker import Broker
from .config import ConfigError, load_config
from .models import Side


def _setup_logging(verbose: bool, log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    file = TimedRotatingFileHandler(log_dir / "bot.log", when="midnight", backupCount=30)
    file.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers[:] = [console, file]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for noisy in ("urllib3", "alpaca"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_check(cfg, broker: Broker, _args) -> int:
    acct = broker.account()
    clock = broker.clock()
    print(f"Mode:            {'PAPER' if cfg.paper else 'LIVE MONEY'}")
    print(f"Account status:  {acct.status}{'  (TRADING BLOCKED)' if acct.trading_blocked else ''}")
    print(f"Equity:          ${acct.equity:,.2f}   (today {acct.day_pnl:+,.2f})")
    print(f"Options BP:      ${acct.options_buying_power:,.2f}")
    print(f"Options level:   {acct.options_level}")
    print(f"Market open:     {clock.is_open}  (next open "
          f"{clock.next_open.astimezone(ET):%a %H:%M ET}, next close "
          f"{clock.next_close.astimezone(ET):%a %H:%M ET})")
    for sym in cfg.symbols:
        print(f"{sym:<6} last:     {broker.last_price(sym):.2f}")
    ok = True
    if acct.options_level < 2:
        print("\n!! Options level must be 2+ to buy calls/puts. Enable options trading in the "
              "Alpaca dashboard (paper accounts: Account -> Options trading).")
        ok = False
    if acct.trading_blocked:
        ok = False
    print("\nAll good - ready to run." if ok else "")
    return 0 if ok else 1


def cmd_chain(cfg, broker: Broker, args) -> int:
    sym = args.symbol.upper()
    spot = broker.last_price(sym)
    today = datetime.now(ET).date()
    side = None if args.type == "both" else Side(args.type)
    chain = broker.option_chain(sym, today, side, spot, args.width)
    if not chain:
        print(f"No {sym} contracts expiring {today} found (is today a trading day?).")
        return 1
    print(f"{sym} spot {spot:.2f}   expiring {today}\n")
    print(f"{'symbol':<22}{'type':<6}{'strike':>8}{'bid':>8}{'ask':>8}{'spread%':>9}{'delta':>8}")
    for c in chain:
        delta = f"{c.delta:+.2f}" if c.delta is not None else "-"
        print(f"{c.symbol:<22}{c.side.value:<6}{c.strike:>8.2f}{c.bid:>8.2f}{c.ask:>8.2f}"
              f"{c.spread_pct:>9.1f}{delta:>8}")
    return 0


def cmd_status(cfg, broker: Broker, _args) -> int:
    acct = broker.account()
    print(f"Equity ${acct.equity:,.2f}  today {acct.day_pnl:+,.2f}\n")
    positions = broker.option_positions()
    if not positions:
        print("No option positions.")
    for p in positions:
        cur = p.current_price or 0
        pct = (cur - p.avg_entry) / p.avg_entry * 100 if p.avg_entry else 0
        print(f"{p.symbol:<22} x{p.qty:<4} entry {p.avg_entry:.2f}  now {cur:.2f}  ({pct:+.1f}%)")
    return 0


def cmd_flatten(cfg, broker: Broker, args) -> int:
    today = datetime.now(ET).date()
    positions = [p for p in broker.option_positions()
                 if p.underlying in cfg.symbols and (args.all or p.expiration == today)]
    if not positions:
        print("Nothing to close.")
        return 0
    for p in positions:
        print(f"  {p.symbol} x{p.qty}")
    if not args.yes and input(f"Close these {len(positions)} position(s) at market? [y/N] ").lower() != "y":
        print("Aborted.")
        return 1
    bot = Bot(cfg, broker)
    bot.load_day(today)
    bot.flatten(positions, "manual flatten")
    return 0


def cmd_run(cfg, broker: Broker, args) -> int:
    try:
        Bot(cfg, broker).run(exit_after_close=args.exit_after_close)
    except KeyboardInterrupt:
        logging.getLogger(__name__).warning(
            "Stopped by user. Any open positions are still open - run `zdte status` / `zdte flatten`.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zdte", description="0DTE ETF options bot for Alpaca")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--config", default="config.yaml", help="config file (default config.yaml)")
    parser.add_argument("--env", default=".env", help="env file with API keys (default .env)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="verify API keys, account and options approval")
    p = sub.add_parser("run", help="start the trading loop")
    p.add_argument("--dry-run", action="store_true",
                   help="use real market data but only simulate orders")
    p.add_argument("--exit-after-close", action="store_true",
                   help="stop once today's session ends (for cron / Task Scheduler)")
    p = sub.add_parser("chain", help="show today's 0DTE chain near the money")
    p.add_argument("symbol")
    p.add_argument("--type", choices=["call", "put", "both"], default="both")
    p.add_argument("--width", type=float, default=1.0, help="strike range around spot, %%")
    sub.add_parser("status", help="show account P&L and option positions")
    p = sub.add_parser("flatten", help="close the bot's 0DTE positions at market")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    p.add_argument("--all", action="store_true", help="include non-0DTE options on configured symbols")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose, Path("logs"))
    try:
        cfg = load_config(args.config, args.env)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    broker = Broker(cfg, dry_run=getattr(args, "dry_run", False))
    commands = {"check": cmd_check, "run": cmd_run, "chain": cmd_chain,
                "status": cmd_status, "flatten": cmd_flatten}
    try:
        return commands[args.command](cfg, broker, args)
    except APIError as e:
        print(f"Alpaca API error: {e}", file=sys.stderr)
        if "forbidden" in str(e).lower() or "unauthorized" in str(e).lower():
            print("Check your API keys in .env, and that ALPACA_PAPER matches the key type.",
                  file=sys.stderr)
        return 1
    except OSError as e:  # requests' connection errors subclass OSError
        print(f"Could not reach Alpaca: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
