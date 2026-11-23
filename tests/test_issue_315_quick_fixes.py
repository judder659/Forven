"""Regression tests for Issue #315 quick fixes:

1. ``start_all.sh`` must remain compatible with macOS's built-in bash 3.2
   (no bash 4+ ``${var,,}`` / ``${var^^}`` case-modification expansions).
2. Coinbase OHLCV ingestion must paginate past its 300-candle per-request cap
   for both ``since_ms`` range downloads and ``limit > 300`` bar-count downloads.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from forven import data as data_mod

REPO_ROOT = Path(__file__).resolve().parents[1]
START_ALL_SH = REPO_ROOT / "start_all.sh"

_BASH4_PATTERNS = (
    (re.compile(r"\$\{[^}]*(?:,,|\^\^)\}"), "bash 4+ case-modification expansion"),
    (re.compile(r"\bdeclare\s+-[a-zA-Z]*A"), "bash 4+ associative array"),
    (re.compile(r"\b(?:mapfile|readarray)\b"), "bash 4+ mapfile/readarray"),
)


def test_start_all_sh_avoids_bash4_only_syntax() -> None:
    text = START_ALL_SH.read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        for pattern, label in _BASH4_PATTERNS:
            assert not pattern.search(line), (
                f"start_all.sh:{lineno} uses {label} (breaks macOS bash 3.2): {line.strip()}"
            )


@pytest.mark.skipif(
    sys.platform.startswith("win") or shutil.which("bash") is None,
    reason="bash syntax check runs on POSIX",
)
def test_start_all_sh_parses_and_normalizes_regime_lab_flag() -> None:
    syntax = subprocess.run(
        ["bash", "-n", str(START_ALL_SH)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert syntax.returncode == 0, syntax.stderr

    # Exercise the exact normalization expression from start_all.sh.
    snippet = (
        'case "$(printf \'%s\' "$VAL" | tr \'[:upper:]\' \'[:lower:]\')" in '
        '1|true|yes|on) printf 1 ;; *) printf 0 ;; esac'
    )
    for raw, expected in (
        ("TRUE", "1"),
        ("Yes", "1"),
        ("ON", "1"),
        ("1", "1"),
        ("0", "0"),
        ("FALSE", "0"),
        ("", "0"),
    ):
        res = subprocess.run(
            ["bash", "-c", snippet],
            env={"VAL": raw, "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode == 0, res.stderr
        assert res.stdout == expected


def _fake_coinbase_exchange(bars: list[list[float]], *, page_cap: int = 300):
    """Fake CCXT Coinbase exchange: returns at most ``page_cap`` candles in
    ``[since, since + page_cap * tf_ms)`` (mirroring CCXT's coinbase.fetch_ohlcv)."""

    class _FakeCoinbase:
        id = "coinbase"
        rateLimit = 0

        def __init__(self) -> None:
            self.calls: list[tuple[int | None, int]] = []

        def fetch_ohlcv(self, symbol: str, timeframe: str = "1h", since: int | None = None, limit: int = 1000):
            tf_ms = data_mod._timeframe_to_ms(timeframe)
            eff = min(int(limit or page_cap), page_cap)
            self.calls.append((since, int(limit or 0)))
            if since is None:
                return bars[-eff:]
            window_end = int(since) + eff * tf_ms
            return [row for row in bars if int(since) <= row[0] < window_end][:eff]

    return _FakeCoinbase()


def _hourly_bars(start_dt: datetime, count: int, *, skip_indices: set[int] | None = None) -> list[list[float]]:
    skip = skip_indices or set()
    rows: list[list[float]] = []
    for i in range(count):
        if i in skip:
            continue
        ts_ms = int((start_dt + timedelta(hours=i)).timestamp() * 1000)
        price = 50_000.0 + i
        rows.append([ts_ms, price, price + 10.0, price - 10.0, price + 5.0, 100.0])
    return rows


def test_venue_ohlcv_page_limit_caps_coinbase_at_300() -> None:
    assert data_mod._venue_ohlcv_page_limit("coinbase") == 300
    assert data_mod._venue_ohlcv_page_limit("COINBASE") == 300
    assert data_mod._venue_ohlcv_page_limit("binance") == data_mod.CHUNK_LIMIT


def test_fetch_range_paginates_coinbase_90_days_even_with_default_chunk_limit() -> None:
    start_dt = datetime(2025, 1, 1, tzinfo=timezone.utc)
    bars = _hourly_bars(start_dt, 2160)
    exchange = _fake_coinbase_exchange(bars, page_cap=300)

    df = data_mod._fetch_range(
        exchange,
        "BTC/USD",
        "1h",
        bars[0][0],
        bars[-1][0],
    )

    assert len(df) == 2160
    assert len(exchange.calls) == 8  # ceil(2160 / 300) = 8 pages


def test_fetch_range_continues_past_mid_window_gap_on_coinbase() -> None:
    start_dt = datetime(2025, 1, 1, tzinfo=timezone.utc)
    # Drop one bar inside page 1 so page 1 returns 299 rows (< 300).
    bars = _hourly_bars(start_dt, 750, skip_indices={120})
    exchange = _fake_coinbase_exchange(bars, page_cap=300)

    df = data_mod._fetch_range(
        exchange,
        "BTC/USD",
        "1h",
        bars[0][0],
        bars[-1][0],
        limit_per_call=300,
    )

    assert len(df) == 749
    assert len(exchange.calls) == 3


def test_fetch_ohlcv_chunked_coinbase_paginates_since_and_limit_requests(tmp_path: Path, monkeypatch) -> None:
    ohlcv_dir = tmp_path / "data" / "ohlcv"
    ohlcv_dir.mkdir(parents=True)
    monkeypatch.setattr(data_mod, "DATA_DIR", ohlcv_dir)
    monkeypatch.setattr(data_mod, "data_root", lambda: tmp_path / "data")
    with data_mod._candle_breakers_lock:
        data_mod._candle_breakers.clear()

    # Anchor well in the past so every bar is closed and never trimmed by _drop_unclosed_bars.
    end_dt = (datetime.now(timezone.utc) - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
    start_dt = end_dt - timedelta(hours=2159)
    bars = _hourly_bars(start_dt, 2160)
    exchange = _fake_coinbase_exchange(bars, page_cap=300)

    monkeypatch.setattr(data_mod, "get_exchange", lambda ex_id: exchange)
    monkeypatch.setattr(
        data_mod,
        "_cached_markets",
        lambda ex: {"BTC/USDT": {"symbol": "BTC/USDT", "active": True}},
    )

    # 1) Explicit 90-day since_ms request must fetch all 2,160 bars in 300-bar pages.
    record = data_mod.fetch_ohlcv_chunked(
        "BTC/USDT",
        "1h",
        exchange_id="coinbase",
        since_ms=bars[0][0],
        until_ms=bars[-1][0],
    )
    assert record["row_count"] == 2160
    assert record["destination"] == "venue"
    assert len(exchange.calls) == 8
    assert all(limit == 300 for _since, limit in exchange.calls)

    # 2) Bar-count request (since_ms=None, limit=1000) must also paginate past 300 bars.
    exchange.calls.clear()
    record_limit = data_mod.fetch_ohlcv_chunked(
        "BTC/USDT",
        "1h",
        exchange_id="coinbase",
        limit=1000,
    )
    assert record_limit["row_count"] >= 1000
    assert len(exchange.calls) >= 4
    assert all(since is not None and limit == 300 for since, limit in exchange.calls)
