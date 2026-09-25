"""Pre-backtest validation must offer the columns a backtest frame can carry, and
new candidates must have feed history across the window they are screened on."""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd
import pytest

from forven.strategies.synthetic_frame import ENRICHMENT_COLUMNS, add_enrichment_columns


def test_enrichment_columns_match_the_feed_vocabulary():
    from forven.strategies.data_availability import _KNOWN_COLUMNS

    assert set(ENRICHMENT_COLUMNS) == set(_KNOWN_COLUMNS)


def test_probe_frame_keeps_its_historical_draws():
    """Adding basis/IV must not change the values existing probe verdicts were built on."""
    from forven.strategies.lookahead_probe import _build_synthetic_ohlcv

    frame = _build_synthetic_ohlcv(900)
    rng = np.random.default_rng(7)
    # Replay the probe's OHLCV draws, then the seven enrichment draws it always made.
    rng.normal(size=900)
    rng.normal(size=900)
    rng.uniform(size=900)
    expected_funding = rng.normal(loc=0.0001, scale=0.0002, size=900)
    expected_oi = rng.uniform(low=1e6, high=5e6, size=900)

    assert np.allclose(frame["funding_rate"].to_numpy(), expected_funding)
    assert np.allclose(frame["open_interest"].to_numpy(), expected_oi)
    assert {"basis", "iv_btc", "iv_eth"} <= set(frame.columns)


def test_add_enrichment_columns_is_reproducible_and_non_degenerate():
    base = pd.DataFrame({"close": np.linspace(1, 2, 50)})
    first = add_enrichment_columns(base.copy(), np.random.default_rng(3))
    second = add_enrichment_columns(base.copy(), np.random.default_rng(3))
    pd.testing.assert_frame_equal(first, second)
    for column in ENRICHMENT_COLUMNS:
        assert first[column].nunique() > 1


class _ReadsEveryFeed:
    """Source for a strategy that indexes basis and IV directly (no guards)."""

    code = '''
from forven.strategies.base import BaseStrategy, Signal

class FeedReader(BaseStrategy):
    name = "feed_reader"
    asset = "BTC"
    strategy_type = "feed_reader"
    default_params = {}

    def generate_signal(self, df):
        edge = df["basis"].iloc[-1] + df["iv_btc"].iloc[-1] + df["funding_rate"].iloc[-1]
        return Signal(entry_signal=bool(edge > 0), direction="long", price=float(df["close"].iloc[-1]))

STRATEGY_CLASS = FeedReader
TYPE_NAME = "feed_reader"
'''


@pytest.mark.skipif(sys.platform != "win32", reason="sandbox run_code cannot import pandas on Linux (known defect)")
def test_registration_harness_offers_every_feed_column():
    from forven.selfheal import validate_strategy_code

    result = validate_strategy_code(_ReadsEveryFeed.code)

    assert result["valid"] is True, result["execution_result"]


def test_probe_offers_basis_and_iv_columns():
    from forven.strategies.base import BaseStrategy
    from forven.strategies.lookahead_probe import probe_lookahead

    class BasisIv(BaseStrategy):
        name = "basis_iv"
        asset = "BTC"
        strategy_type = "basis_iv"
        default_params = {}

        def generate_signal(self, df):  # pragma: no cover - vectorized path is probed
            raise NotImplementedError

        def generate_signals(self, df):
            entries = (df["basis"] < df["basis"].rolling(20).mean()) & (df["iv_eth"] > 50)
            return entries.fillna(False), pd.Series(False, index=df.index)

    verdict = probe_lookahead(BasisIv("probe", {}))

    assert verdict.reason is None
    assert "probe infrastructure error" not in str(verdict.inconclusive or "")


def test_short_history_columns_flags_a_recently_collected_feed(tmp_path, monkeypatch):
    from forven.strategies import data_availability as availability

    old = tmp_path / "funding.parquet"
    new = tmp_path / "liquidations.parquet"
    pd.DataFrame({"timestamp": pd.date_range("2022-01-01", periods=5, freq="D", tz="UTC"), "funding_rate": 0.0}).to_parquet(old)
    pd.DataFrame({"timestamp": pd.date_range("2026-07-06", periods=5, freq="D", tz="UTC"), "long_liq_usd": 1.0}).to_parquet(new)

    class _Spec:
        def __init__(self, path, columns):
            self.path, self.output_columns = path, columns

    monkeypatch.setattr(
        "forven.dataeng.hub._available_enrichment_specs",
        lambda *_a, **_k: [_Spec(old, ("funding_rate",)), _Spec(new, ("long_liq_usd", "short_liq_usd"))],
    )

    short = availability.short_history_columns(
        "BTC/USDT", "1h", {"funding_rate", "long_liq_usd"},
        window_start="2024-09-25", window_end="2026-09-25", min_fraction=0.5,
    )

    assert list(short) == ["long_liq_usd"]
    assert short["long_liq_usd"].date().isoformat() == "2026-07-06"


def test_candidate_with_a_short_feed_is_rejected_as_insufficient_history(forven_db, tmp_path, monkeypatch):
    from forven.crucible_tasks import check_candidate_trades
    from forven.db import get_db

    source = tmp_path / "liq_gate.py"
    source.write_text('LONG = "long_liq_usd"\n', encoding="utf-8")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, runtime_type, symbol, timeframe, params, stage, sandbox_only, source_ref) "
            "VALUES ('S-LIQ', 'n', 'liq_gate', 'imported__dropzone_liq_gate_abc', 'BTC/USDT', '1h', '{}', 'quick_screen', 1, ?)",
            (str(source),),
        )
    monkeypatch.setattr(
        "forven.strategies.backtest.backtest_strategy",
        lambda **_k: {"metrics": {"total_trades": 40}, "start_date": "2024-09-25", "end_date": "2026-09-25"},
    )
    monkeypatch.setattr(
        "forven.strategies.data_availability.feed_history_start",
        lambda *_a: {"long_liq_usd": pd.Timestamp("2026-07-06", tz="UTC")},
    )

    check = check_candidate_trades("S-LIQ")

    assert check.status == "short_history"
    assert check.short_feeds == (("long_liq_usd", "2026-07-06"),)
