"""Streaming manager primitives for app-open live capture."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from forven.dataeng.identity import SymbolRef, to_ref


@dataclass(frozen=True)
class StreamState:
    source: str
    market: str
    symbol: str
    stream: str
    status: str
    buffered_rows: int
    updated_at: str


class StreamManager:
    def __init__(self, buffer_limit: int = 10_000) -> None:
        self.buffer_limit = max(1, int(buffer_limit))
        self._buffers: dict[tuple[str, str, str, str], deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=self.buffer_limit)
        )
        self._states: dict[tuple[str, str, str, str], str] = {}

    def ingest(self, ref: str | SymbolRef, stream: str, rows: pd.DataFrame | list[dict[str, Any]]) -> None:
        resolved = ref if isinstance(ref, SymbolRef) else to_ref(ref)
        key = (resolved.source, resolved.market, resolved.to_fs(), stream)
        frame = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
        for record in frame.to_dict("records"):
            self._buffers[key].append(record)
        self._states[key] = "connected"

    def disconnect(self, ref: str | SymbolRef, stream: str) -> None:
        resolved = ref if isinstance(ref, SymbolRef) else to_ref(ref)
        key = (resolved.source, resolved.market, resolved.to_fs(), stream)
        self._states[key] = "disconnected"

    def flush_closed_candles(self, ref: str | SymbolRef, timeframe: str, *, now: object | None = None) -> int:
        from forven.data import _get_dataset_lock, _timeframe_to_ms, load_parquet, merge_and_dedup, save_parquet

        resolved = ref if isinstance(ref, SymbolRef) else to_ref(ref, timeframe=timeframe)
        key = (resolved.source, resolved.market, resolved.to_fs(), "candles")
        buffer = self._buffers.get(key)
        if not buffer:
            return 0

        now_ts = _as_utc(now or datetime.now(timezone.utc))
        close_delta = pd.Timedelta(milliseconds=_timeframe_to_ms(timeframe))
        buffered = pd.DataFrame(list(buffer))
        if buffered.empty or "timestamp" not in buffered.columns:
            return 0
        buffered["timestamp"] = pd.to_datetime(buffered["timestamp"], utc=True, errors="coerce")
        closed = buffered[buffered["timestamp"] + close_delta <= now_ts].copy()
        if closed.empty:
            return 0

        open_rows = buffered[buffered["timestamp"] + close_delta > now_ts].copy()
        buffer.clear()
        for record in open_rows.to_dict("records"):
            buffer.append(record)

        # Same per-series lock the fetch/backfill writers hold: an unlocked
        # load→merge→save here interleaving with a concurrent backfill would
        # lose whichever write finished first (last os.replace wins).
        with _get_dataset_lock(resolved.to_fs(), timeframe):
            existing = load_parquet(resolved.to_fs(), timeframe)
            merged = merge_and_dedup(existing, closed)
            save_parquet(merged, resolved.to_fs(), timeframe, source=resolved.source)
        return max(0, len(merged) - (len(existing) if existing is not None else 0))

    def status(self) -> list[StreamState]:
        rows: list[StreamState] = []
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for key, buffer in sorted(self._buffers.items()):
            source, market, symbol, stream = key
            rows.append(
                StreamState(
                    source=source,
                    market=market,
                    symbol=symbol,
                    stream=stream,
                    status=self._states.get(key, "unknown"),
                    buffered_rows=len(buffer),
                    updated_at=now,
                )
            )
        return rows


_STREAM_MANAGER: StreamManager | None = None


def get_stream_manager() -> StreamManager:
    global _STREAM_MANAGER
    if _STREAM_MANAGER is None:
        _STREAM_MANAGER = StreamManager()
    return _STREAM_MANAGER


def _as_utc(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")
