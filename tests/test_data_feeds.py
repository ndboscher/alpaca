"""Paid-plan data handling: stale-quote filter, feed checks, poll-rate validation."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
from alpaca.common.exceptions import APIError

from zdte_bot.broker import Broker
from zdte_bot.config import Config, ConfigError, load_config

TODAY = date(2026, 10, 6)


def make_broker(**cfg_kw) -> Broker:
    return Broker(Config(api_key="k", secret_key="s", **cfg_kw))


def snap(age_seconds: float):
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return NS(latest_quote=NS(bid_price=1.00, ask_price=1.04, timestamp=ts),
              greeks=NS(delta=0.40))


def test_option_chain_drops_stale_quotes():
    b = make_broker(max_quote_age_seconds=30)
    b.options.get_option_chain = lambda req: {
        "SPY261006C00500000": snap(2),     # fresh
        "SPY261006C00501000": snap(300),   # five minutes old
    }
    fresh = b.option_chain("SPY", TODAY, None, 500, 1.0)
    assert [c.symbol for c in fresh] == ["SPY261006C00500000"]
    every = b.option_chain("SPY", TODAY, None, 500, 1.0, fresh_only=False)
    assert len(every) == 2


def test_check_feeds_reports_rejected_subscription():
    b = make_broker()

    def rejected(_req):
        raise APIError('{"message":"subscription does not permit querying recent SIP data"}')

    b.stocks.get_stock_latest_trade = rejected
    b.options.get_option_chain = lambda req: {}
    problems = b.check_feeds()
    assert len(problems) == 1 and "stock feed 'sip'" in problems[0]


def test_check_feeds_ok():
    b = make_broker()
    b.stocks.get_stock_latest_trade = lambda req: {"SPY": NS(price=500.0)}
    b.options.get_option_chain = lambda req: {}
    assert b.check_feeds() == []


def test_fast_polling_requires_paid_feeds(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("stock_feed: iex\noptions_feed: indicative\npoll_seconds: 5\n")
    with pytest.raises(ConfigError, match="paid data plan"):
        load_config(p, tmp_path / "none.env", require_keys=False)
    p.write_text("stock_feed: iex\noptions_feed: indicative\npoll_seconds: 15\n")
    assert load_config(p, tmp_path / "none.env", require_keys=False).poll_seconds == 15


def test_defaults_are_paid_plan(tmp_path):
    cfg = load_config("config.example.yaml",
                      tmp_path / "none.env", require_keys=False)
    assert (cfg.stock_feed, cfg.options_feed, cfg.poll_seconds) == ("sip", "opra", 5)
