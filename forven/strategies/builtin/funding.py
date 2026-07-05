"""Funding Rate Mean Reversion strategy — S027."""

import pandas as pd

from forven.strategies.base import BaseStrategy, Signal

TYPE_NAME = "funding"


class FundingStrategy(BaseStrategy):

    @property
    def name(self) -> str:
        return f"Funding Rate Mean Reversion ({self.asset})"

    @property
    def asset(self) -> str:
        return self.params.get("_asset", "BTC")

    @property
    def strategy_type(self) -> str:
        return TYPE_NAME

    @property
    def default_params(self) -> dict:
        # The enriched ``funding_rate`` column is a PER-HOUR rate (HL is natively
        # hourly; Binance 8h rates are divided by 8 — one convention across both
        # sources, see market_data.fetch_binance_funding_series). These thresholds
        # are the original per-8h calibration (0.003% / 0.001%) divided by 8: read
        # against the per-hour column, the per-8h values only fired on ~0.5% of
        # funding events in 6.5y of BTC/ETH history and the strategy never traded.
        return {
            "entry_threshold": 0.00000375, "exit_threshold": 0.00000125,
            "regime_ema200": True, "leverage": 3.0,
        }

    @property
    def compatible_regimes(self) -> set[str]:
        return {"RANGE_BOUND"}

    def describe(self) -> str:
        p = self.params
        threshold_pct = p.get("entry_threshold", 0.00000375) * 100
        return (
            f"Buys when crypto futures funding becomes extremely negative "
            f"(shorts overpaying longs, below -{threshold_pct:.4f}%). "
            f"Exits when funding normalizes."
        )

    def generate_signals(self, df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        """Vectorized twin of ``generate_signal`` — the SINGLE source of entry/exit
        logic so the backtest and the live/paper scanner trade the identical signal
        set. ``generate_signal`` delegates here for its boolean decision.

        Entry: the funding rate is extremely negative (below ``-entry_threshold``)
        while price is above the 200-EMA regime filter. Exit: funding normalizes
        back above ``-exit_threshold``. When no ``funding_rate`` column is present
        there is nothing to vectorize, so the signals are all-False — matching the
        per-bar method's neutral return when funding data is unavailable.
        """
        p = self.params
        close = df["close"]
        entry_threshold = p.get("entry_threshold", 0.00000375)
        exit_threshold = p.get("exit_threshold", 0.00000125)

        if "funding_rate" not in df.columns:
            empty = pd.Series(False, index=df.index)
            return empty, empty.copy()

        ema200 = close.ewm(span=200, adjust=False).mean()
        funding = df["funding_rate"]
        regime_ok = close > ema200

        entry = (funding < -entry_threshold) & regime_ok
        exit_ = funding > -exit_threshold
        return entry.fillna(False), exit_.fillna(False)

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """Funding rate mean reversion — reads df['funding_rate'] (enriched by the
        parent: scanner/backtest add it by asset). Neutral when the column is absent."""
        from forven.strategies.indicators import atr

        p = self.params
        close = df["close"]
        ema200 = close.ewm(span=200, adjust=False).mean()
        atr_14 = atr(df, 14)

        curr_close = float(close.iloc[-1])
        curr_ema200 = float(ema200.iloc[-1])
        curr_atr = float(atr_14.iloc[-1])

        # Funding comes from the parent-enriched ``funding_rate`` column — the scanner
        # and backtest both enrich it by asset before delegating. A strategy must not
        # fetch its own market data: that breaks backtest/live parity and is denied in
        # the isolated worker anyway (R3 — forven.strategies.sentiment is no longer on
        # the untrusted import allowlist). When the column is absent there is simply no
        # funding signal, so fall through to the neutral return below.
        funding = None
        if "funding_rate" in df.columns:
            funding = float(df["funding_rate"].iloc[-1])

        # If no funding data available, return neutral signal
        if funding is None:
            return Signal(
                price=round(curr_close, 4),
                direction="long",
                indicators={"funding": 0, "ema200": round(curr_ema200, 4), "atr_14": round(curr_atr, 6), "adx": 0},
            )

        regime_ok = curr_close > curr_ema200

        # Single source of truth for the decision (keeps per-bar and vectorized in
        # lockstep). In live mode the funding rate comes from a live fetch rather
        # than a df column, so inject it before delegating to the vectorized twin.
        signal_df = df
        if "funding_rate" not in df.columns:
            signal_df = df.copy()
            signal_df["funding_rate"] = funding
        entries, exits = self.generate_signals(signal_df)

        return Signal(
            entry_signal=bool(entries.iloc[-1]), exit_signal=bool(exits.iloc[-1]),
            price=round(curr_close, 4), direction="long",
            indicators={
                "funding": funding,
                "ema200": round(curr_ema200, 4),
                "atr_14": round(curr_atr, 6),
                "adx": 0,
                "regime_ok": bool(regime_ok),
            },
        )


STRATEGY_CLASS = FundingStrategy

STRATEGIES = [
    ("S027-FUND-BTC", FundingStrategy, {"_asset": "BTC"}),
]
