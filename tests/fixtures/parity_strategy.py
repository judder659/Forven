"""Deterministic, causal strategy used on both sides of the worker boundary."""

import pandas as pd

from forven.strategies.base import BaseStrategy, DirectionalSignals, Signal

TYPE_NAME = "parity_fixture_type"


class ParityStrategy(BaseStrategy):
    @property
    def name(self) -> str:
        return "Parity fixture"

    @property
    def asset(self) -> str:
        return "BTC"

    @property
    def strategy_type(self) -> str:
        return TYPE_NAME

    @property
    def default_params(self) -> dict:
        return {}

    def generate_signals(self, df: pd.DataFrame) -> DirectionalSignals:
        mean = df["close"].rolling(5).mean()
        zero = pd.Series(False, index=df.index)
        return DirectionalSignals(
            long_entries=(df["close"] > mean).fillna(False),
            long_exits=(df["close"] < mean).fillna(False),
            short_entries=zero, short_exits=zero,
        )

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        signals = self.generate_signals(df)
        return Signal(
            entry_signal=bool(signals.long_entries.iloc[-1]),
            exit_signal=bool(signals.long_exits.iloc[-1]),
            price=float(df["close"].iloc[-1]), direction="long",
        )


STRATEGY_CLASS = ParityStrategy
