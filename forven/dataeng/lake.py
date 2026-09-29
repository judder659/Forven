"""One enumeration of every stored series in the lake, from parquet footers.

The catalog, the SLA census/collector and the storage inventory all need
"what series exist, how many rows, first/last bar, size, last write" for
every stream. This module answers it once, from footer statistics only
(never a column load), cached per file by (size, mtime) so a repeat call over
~1,000 files costs a stat() each.

Layout (relative to ``forven.data.data_root()``):

    ohlcv/{SYM}/{tf}.parquet (+ .tail)                  stream=ohlcv   venue=canonical
    ohlcv/source={src}/market={mkt}/{SYM}/{tf}.parquet  stream=ohlcv   venue={src}:{mkt}
    funding/{SYM}/history.parquet                       stream=funding venue=canonical (cadence from footer)
    funding_hl/{COIN}/{tf}.parquet                      stream=funding venue=hyperliquid:perp symbol={COIN}-USDT
    oi/{SYM}/{tf}.parquet                               stream=oi
    basis/{SYM}/{tf}.parquet                            stream=basis
    derivatives/{SYM}/long_short_ratio_{tf}.parquet     stream=ls_ratio
    derivatives/{SYM}/taker_volume_{tf}.parquet         stream=taker
    derivatives/{SYM}/liquidations_{tf}.parquet         stream=liquidations
    volatility/dvol_{ccy}_{tf}.parquet                  stream=iv      symbol={CCY}

Backups (*.bak), temp files (*.tmp) and dot-directories are never series.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

log = logging.getLogger("forven.dataeng.lake")

STREAMS: tuple[str, ...] = ("ohlcv", "funding", "oi", "basis", "iv", "ls_ratio", "taker", "liquidations")
CANONICAL_VENUE = "canonical"

_DERIVATIVE_FILES = {
    "long_short_ratio": "ls_ratio",
    "taker_volume": "taker",
    "liquidations": "liquidations",
}
_TIMEFRAME_RE = re.compile(r"^\d+[mhdwM]$")
_FUNDING_CADENCES_H = (1.0, 4.0, 8.0)


@dataclass(frozen=True)
class SeriesFile:
    """One stored series (cold file plus any tail sidecar), from footers."""

    stream: str
    venue: str
    symbol: str
    timeframe: str
    path: Path
    tail_path: Path | None
    rows: int
    first_ms: int | None
    last_ms: int | None
    size_bytes: int
    mtime: float
    source: str | None
    market: str | None

    @property
    def id(self) -> str:
        return f"{self.stream}:{self.venue}:{self.symbol}:{self.timeframe}"

    @property
    def paths(self) -> tuple[Path, ...]:
        return (self.path,) if self.tail_path is None else (self.path, self.tail_path)


_cache_lock = threading.Lock()
# path -> ((size, mtime_ns), (rows, first_ms, last_ms, source, market))
_footer_cache: dict[str, tuple[tuple[int, int], tuple[int, int | None, int | None, str | None, str | None]]] = {}


def _stat_key(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (int(st.st_size), int(st.st_mtime_ns))


def _footer(path: Path) -> tuple[int, int | None, int | None, str | None, str | None] | None:
    key = _stat_key(path)
    if key is None:
        return None
    cache_key = str(path)
    with _cache_lock:
        hit = _footer_cache.get(cache_key)
        if hit is not None and hit[0] == key:
            return hit[1]
    try:
        import pyarrow.parquet as pq

        from forven.data import _footer_bounds

        rows, first_ms, last_ms = _footer_bounds(path)
        meta = pq.read_metadata(path).metadata or {}
        source = meta.get(b"forven_source")
        market = meta.get(b"forven_market")
        value = (
            int(rows),
            first_ms,
            last_ms,
            source.decode("utf-8", errors="ignore") if source else None,
            market.decode("utf-8", errors="ignore") if market else None,
        )
    except Exception as exc:
        log.debug("unreadable parquet footer %s: %s", path, exc)
        return None
    with _cache_lock:
        _footer_cache[cache_key] = (key, value)
    return value


def _funding_cadence(rows: int, first_ms: int | None, last_ms: int | None) -> str:
    """Nearest of 1h/4h/8h to the file's average print spacing."""
    if rows > 1 and first_ms is not None and last_ms is not None and last_ms > first_ms:
        hours = (last_ms - first_ms) / 3_600_000.0 / (rows - 1)
        best = min(_FUNDING_CADENCES_H, key=lambda cadence: abs(cadence - hours))
        return f"{int(best)}h"
    return "8h"


def _build(
    stream: str,
    venue: str,
    symbol: str,
    timeframe: str,
    path: Path,
    tail: Path | None = None,
) -> SeriesFile | None:
    head = _footer(path)
    if head is None:
        return None
    rows, first_ms, last_ms, source, market = head
    try:
        st = path.stat()
    except OSError:
        return None
    size = int(st.st_size)
    mtime = float(st.st_mtime)
    tail_used: Path | None = None
    if tail is not None and tail.exists():
        tail_meta = _footer(tail)
        if tail_meta is not None:
            tail_used = tail
            t_rows, t_first, t_last, _t_source, _t_market = tail_meta
            rows += t_rows
            if t_first is not None and (first_ms is None or t_first < first_ms):
                first_ms = t_first
            if t_last is not None and (last_ms is None or t_last > last_ms):
                last_ms = t_last
            try:
                tail_st = tail.stat()
                size += int(tail_st.st_size)
                mtime = max(mtime, float(tail_st.st_mtime))
            except OSError:
                pass
    if stream == "funding" and venue == CANONICAL_VENUE:
        timeframe = _funding_cadence(rows, first_ms, last_ms)
    return SeriesFile(
        stream=stream,
        venue=venue,
        symbol=symbol,
        timeframe=timeframe,
        path=path,
        tail_path=tail_used,
        rows=rows,
        first_ms=first_ms,
        last_ms=last_ms,
        size_bytes=int(size),
        mtime=float(mtime),
        source=source,
        market=market,
    )


def _dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))


def _ohlcv(root: Path) -> Iterable[SeriesFile]:
    ohlcv = root / "ohlcv"
    for sym_dir in _dirs(ohlcv):
        if sym_dir.name.startswith("source="):
            source = sym_dir.name.split("=", 1)[1]
            for market_dir in _dirs(sym_dir):
                if not market_dir.name.startswith("market="):
                    continue
                venue = f"{source}:{market_dir.name.split('=', 1)[1]}"
                for venue_sym in _dirs(market_dir):
                    for f in sorted(venue_sym.glob("*.parquet")):
                        built = _build("ohlcv", venue, venue_sym.name, f.stem, f)
                        if built is not None:
                            yield built
            continue
        for f in sorted(sym_dir.glob("*.parquet")):
            tail = f.with_name(f.name + ".tail")
            built = _build("ohlcv", CANONICAL_VENUE, sym_dir.name, f.stem, f, tail if tail.exists() else None)
            if built is not None:
                yield built


def _per_symbol_tf(root: Path, folder: str, stream: str) -> Iterable[SeriesFile]:
    for sym_dir in _dirs(root / folder):
        for f in sorted(sym_dir.glob("*.parquet")):
            if _TIMEFRAME_RE.match(f.stem):
                built = _build(stream, CANONICAL_VENUE, sym_dir.name, f.stem, f)
                if built is not None:
                    yield built


def _funding(root: Path) -> Iterable[SeriesFile]:
    for sym_dir in _dirs(root / "funding"):
        f = sym_dir / "history.parquet"
        if f.exists():
            built = _build("funding", CANONICAL_VENUE, sym_dir.name, "8h", f)
            if built is not None:
                yield built
    for coin_dir in _dirs(root / "funding_hl"):
        for f in sorted(coin_dir.glob("*.parquet")):
            if _TIMEFRAME_RE.match(f.stem):
                built = _build("funding", "hyperliquid:perp", f"{coin_dir.name}-USDT", f.stem, f)
                if built is not None:
                    yield built


def _derivatives(root: Path) -> Iterable[SeriesFile]:
    for sym_dir in _dirs(root / "derivatives"):
        for f in sorted(sym_dir.glob("*.parquet")):
            prefix, _, timeframe = f.stem.rpartition("_")
            stream = _DERIVATIVE_FILES.get(prefix)
            if stream and _TIMEFRAME_RE.match(timeframe):
                built = _build(stream, CANONICAL_VENUE, sym_dir.name, timeframe, f)
                if built is not None:
                    yield built


def _iv(root: Path) -> Iterable[SeriesFile]:
    vol = root / "volatility"
    if not vol.is_dir():
        return
    for f in sorted(vol.glob("dvol_*_*.parquet")):
        parts = f.stem.split("_")
        if len(parts) == 3 and _TIMEFRAME_RE.match(parts[2]):
            built = _build("iv", "deribit:index", parts[1].upper(), parts[2], f)
            if built is not None:
                yield built


def enumerate_series(
    *,
    streams: Iterable[str] | None = None,
    root: Path | str | None = None,
) -> list[SeriesFile]:
    """Every stored series, sorted by (stream, venue, symbol, timeframe).
    ``streams`` limits the walk (e.g. ``("ohlcv",)``)."""
    if root is None:
        from forven.data import data_root

        base = Path(data_root())
    else:
        base = Path(root)
    wanted = set(streams) if streams is not None else set(STREAMS)
    found: list[SeriesFile] = []
    if "ohlcv" in wanted:
        found.extend(_ohlcv(base))
    if "funding" in wanted:
        found.extend(_funding(base))
    if "oi" in wanted:
        found.extend(_per_symbol_tf(base, "oi", "oi"))
    if "basis" in wanted:
        found.extend(_per_symbol_tf(base, "basis", "basis"))
    if wanted & {"ls_ratio", "taker", "liquidations"}:
        found.extend(s for s in _derivatives(base) if s.stream in wanted)
    if "iv" in wanted:
        found.extend(_iv(base))
    found.sort(key=lambda s: (s.stream, s.venue, s.symbol, s.timeframe))
    return found


def find_series(symbol: str, timeframe: str, *, stream: str = "ohlcv", venue: str = CANONICAL_VENUE) -> SeriesFile | None:
    for series in enumerate_series(streams=(stream,)):
        if series.symbol == symbol and series.timeframe == timeframe and series.venue == venue:
            return series
    return None


def clear_footer_cache() -> None:
    with _cache_lock:
        _footer_cache.clear()


def footer_cache_size() -> int:
    with _cache_lock:
        return len(_footer_cache)


__all__ = [
    "CANONICAL_VENUE",
    "STREAMS",
    "SeriesFile",
    "clear_footer_cache",
    "enumerate_series",
    "find_series",
    "footer_cache_size",
]

