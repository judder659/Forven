"""A number passed as text for a numeric parameter is read as that number.

Agents pass ``{"atr_len": "14"}`` to the backtest tools, and two paper
strategies on one install stored all seven of their numeric parameters as
text. The strategy then failed in its indicators with
``'>' not supported between instances of 'int' and 'str'``.
"""

import pandas as pd

from forven.strategies.base import BaseStrategy, Signal, _numeric_text_overrides


class _Breakout(BaseStrategy):
    name = "text params"
    asset = "BTC"
    strategy_type = "text_params_test"
    default_params = {
        "atr_len": 14,
        "risk_reward": 1.5,
        "use_filter": True,
        "mode": "ema",
        "label": "7",
    }

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        return Signal(entry_signal=len(df) >= self.params["atr_len"] and self.params["risk_reward"] > 1)


def test_numeric_text_is_read_by_the_default_type():
    strategy = _Breakout("S-TEXT", {"atr_len": "14", "risk_reward": " 2 "})

    assert strategy.params["atr_len"] == 14 and isinstance(strategy.params["atr_len"], int)
    assert strategy.params["risk_reward"] == 2.0 and isinstance(strategy.params["risk_reward"], float)
    assert strategy.p.atr_len == 14
    # The comparison that used to raise TypeError.
    assert strategy.generate_signal(pd.DataFrame({"close": range(20)})).entry_signal is True


def test_whole_number_text_for_an_int_parameter_stays_an_int():
    assert _Breakout("S-TEXT", {"atr_len": "20.0"}).params["atr_len"] == 20
    assert isinstance(_Breakout("S-TEXT", {"atr_len": "20.0"}).params["atr_len"], int)
    # A fractional value is not rounded for the strategy.
    assert _Breakout("S-TEXT", {"atr_len": "20.5"}).params["atr_len"] == 20.5


def test_everything_else_is_passed_through_untouched():
    overrides = {
        "atr_len": "auto",       # not a number
        "risk_reward": "nan",    # not a finite number
        "use_filter": "1",       # the default is a flag, not a number
        "mode": "sma",           # the default is text
        "label": "9",            # the default is text that looks like a number
        "extra": "3",            # no default to read the type from
    }

    assert _numeric_text_overrides(_Breakout.default_params, overrides) == overrides
    assert _Breakout("S-TEXT", overrides).params == {**_Breakout.default_params, **overrides}


def test_native_numbers_and_missing_overrides_are_unchanged():
    assert _Breakout("S-TEXT").params == _Breakout.default_params
    params = _Breakout("S-TEXT", {"atr_len": 21, "risk_reward": 3}).params
    assert params["atr_len"] == 21 and params["risk_reward"] == 3 and isinstance(params["risk_reward"], int)


def test_the_callers_dict_is_not_modified():
    given = {"atr_len": "14"}
    _Breakout("S-TEXT", given)
    assert given == {"atr_len": "14"}
