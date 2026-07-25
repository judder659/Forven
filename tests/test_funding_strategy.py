from __future__ import annotations

import pandas as pd

from forven.strategies.builtin.funding import FundingStrategy


def _sample_ohlcv(rows: int = 240, funding_rate: float | None = None) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=rows, freq="h", tz="UTC")
    close = pd.Series([100.0 + i * 0.5 for i in range(rows)], index=idx)
    open_ = close.shift(1).fillna(close.iloc[0])
    high = pd.Series([max(o, c) + 0.2 for o, c in zip(open_, close)], index=idx)
    low = pd.Series([min(o, c) - 0.2 for o, c in zip(open_, close)], index=idx)
    volume = pd.Series([1000.0 + i for i in range(rows)], index=idx)
    frame = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )
    if funding_rate is not None:
        # The parent enriches this column; it is a PER-HOUR rate (HL is natively
        # hourly, Binance 8h rates are divided by 8 — one convention across both).
        frame["funding_rate"] = funding_rate
    return frame


def test_funding_strategy_reads_the_enriched_funding_rate_column():
    """Deeply negative funding + price above the 200-EMA regime filter -> entry.

    The strategy no longer imports forven.strategies.sentiment (that module is not
    on the strategy-facing import allowlist); funding arrives as the enriched
    ``funding_rate`` DataFrame column instead. A fixture that patches
    sentiment.fetch_funding_rates therefore controls nothing — the column stays
    absent and the strategy returns its neutral no-funding-data signal.
    """
    strategy = FundingStrategy("S027-FUND-BTC", {"_asset": "BTC"})
    # entry_threshold defaults to 0.00000375/hr; sit clearly below -threshold.
    signal = strategy.generate_signal(_sample_ohlcv(funding_rate=-0.0002))

    assert signal.price > 0
    assert signal.entry_signal is True
    assert signal.exit_signal is False
    assert float(signal.indicators.get("funding")) == -0.0002
    assert signal.indicators["regime_ok"] is True


def test_funding_strategy_exits_when_funding_normalizes():
    strategy = FundingStrategy("S027-FUND-BTC", {"_asset": "BTC"})
    # Above -exit_threshold (0.00000125/hr): the mean-reversion edge is gone.
    signal = strategy.generate_signal(_sample_ohlcv(funding_rate=0.00005))

    assert signal.entry_signal is False
    assert signal.exit_signal is True


def test_funding_strategy_is_neutral_without_funding_data():
    """No enriched column -> no funding signal at all (never a fabricated entry)."""
    strategy = FundingStrategy("S027-FUND-BTC", {"_asset": "BTC"})
    signal = strategy.generate_signal(_sample_ohlcv())

    assert signal.entry_signal is False
    assert signal.exit_signal is False
    assert float(signal.indicators.get("funding")) == 0


def test_funding_strategy_regime_filter_blocks_entry_below_the_ema():
    """Negative funding alone is not enough — price must be above the 200-EMA."""
    frame = _sample_ohlcv(funding_rate=-0.0002)
    # Collapse the last close far below the EMA200 so regime_ok is False.
    frame.loc[frame.index[-1], "close"] = 1.0

    strategy = FundingStrategy("S027-FUND-BTC", {"_asset": "BTC"})
    signal = strategy.generate_signal(frame)

    assert signal.indicators["regime_ok"] is False
    assert signal.entry_signal is False
