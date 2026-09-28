"""Canonical data-engine series identity helpers."""

from __future__ import annotations

from dataclasses import dataclass


_DEFAULT_SOURCE = "binance"
_DEFAULT_MARKET = "spot"
_QUOTE_SUFFIXES = ("USDT", "USDC", "BUSD", "USD", "BTC", "ETH", "BNB", "EUR")
_MARKET_ALIASES = {
    "": _DEFAULT_MARKET,
    "cash": "spot",
    "spot": "spot",
    "perp": "perp",
    "perps": "perp",
    "swap": "perp",
    "future": "perp",
    "futures": "perp",
}


def _clean_token(value: object, *, lower: bool = False) -> str:
    text = str(value or "").strip()
    return text.lower() if lower else text.upper()


def _normalize_market(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return _MARKET_ALIASES.get(normalized, normalized or _DEFAULT_MARKET)


def _split_symbol(raw: str, *, split_bare: bool) -> tuple[str, str | None]:
    symbol = raw.strip().upper().replace("_", "-")
    if not symbol:
        return "", None

    if ":" in symbol:
        symbol, _, _settlement = symbol.partition(":")

    inferred_market: str | None = None
    for suffix in ("-PERP", "-SWAP", " PERP", " SWAP"):
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
            inferred_market = "perp"
            break

    if "/" in symbol:
        base, quote = symbol.split("/", 1)
        return f"{base}-{quote}", inferred_market

    if "-" in symbol:
        base, quote = symbol.split("-", 1)
        return f"{base}-{quote}", inferred_market

    if split_bare:
        for quote in _QUOTE_SUFFIXES:
            if symbol.endswith(quote) and len(symbol) > len(quote):
                return f"{symbol[:-len(quote)]}-{quote}", inferred_market

    return symbol, inferred_market


@dataclass(frozen=True)
class SymbolRef:
    """Canonical key for one market-data series."""

    source: str
    market: str
    symbol: str
    timeframe: str | None = None

    @classmethod
    def parse(
        cls,
        symbol: str | "SymbolRef",
        *,
        source: str = _DEFAULT_SOURCE,
        market: str = _DEFAULT_MARKET,
        timeframe: str | None = None,
        split_bare: bool = False,
    ) -> "SymbolRef":
        if isinstance(symbol, SymbolRef):
            if source == _DEFAULT_SOURCE and market == _DEFAULT_MARKET and timeframe is None:
                return symbol
            return cls(
                source=_clean_token(source or symbol.source, lower=True),
                market=_normalize_market(market or symbol.market),
                symbol=symbol.symbol,
                timeframe=str(timeframe or symbol.timeframe) if (timeframe or symbol.timeframe) else None,
            )

        normalized_symbol, inferred_market = _split_symbol(str(symbol or ""), split_bare=split_bare)
        resolved_market = _normalize_market(inferred_market or market)
        return cls(
            source=_clean_token(source or _DEFAULT_SOURCE, lower=True),
            market=resolved_market,
            symbol=normalized_symbol,
            timeframe=str(timeframe).strip() if timeframe is not None else None,
        )

    def to_fs(self) -> str:
        return self.symbol.replace("/", "-").replace("_", "-").upper()

    def to_ccxt(self) -> str:
        fs_symbol = self.to_fs()
        if "-" not in fs_symbol:
            return fs_symbol
        base, quote = fs_symbol.split("-", 1)
        if self.market == "perp":
            return f"{base}/{quote}:{quote}"
        return f"{base}/{quote}"

    def key(self) -> str:
        tf = self.timeframe or "*"
        return f"{self.source}:{self.market}:{self.to_fs()}:{tf}"


def to_ref(
    symbol: str | SymbolRef,
    *,
    source: str = _DEFAULT_SOURCE,
    market: str = _DEFAULT_MARKET,
    timeframe: str | None = None,
    split_bare: bool = False,
) -> SymbolRef:
    return SymbolRef.parse(
        symbol,
        source=source,
        market=market,
        timeframe=timeframe,
        split_bare=split_bare,
    )


def to_fs(symbol: str | SymbolRef, **kwargs: object) -> str:
    return to_ref(symbol, **kwargs).to_fs()


def to_ccxt(symbol: str | SymbolRef, **kwargs: object) -> str:
    return to_ref(symbol, **kwargs).to_ccxt()


# ---------------------------------------------------------------- resolve & audit
#
# One instrument can be spelled many ways (BTC, BTCUSDT, BTC/USDT, BTC-USDT,
# BTC/USDT:USDT). The lake stores BASE-QUOTE directories; ``resolve_symbol``
# maps any spelling to the instruments it may mean, where they are listed and
# what is stored, and ``audit_identity`` reports lake folders whose spelling or
# content does not fit (report only — nothing is moved).

_STABLE_QUOTES = ("USDT", "USDC")
_CRYPTO_QUOTES = ("USDT", "USDC", "FDUSD", "BUSD", "BTC", "ETH", "BNB")
# Quote suffixes a bare spelling (BTCUSDT) is split on. Fiat codes such as TRY
# are left out: "RETRY" is not RE/TRY.
_SPLIT_QUOTES = ("FDUSD", "USDT", "USDC", "BUSD", "USD", "BTC", "ETH", "BNB")
_STREAM_DIRS = ("funding", "oi", "basis", "derivatives")
_RECENT_WRITE_SECONDS = 7 * 86400


def split_pair(text: str) -> tuple[str, str | None]:
    """(base, quote) of any spelling; quote None for a bare base."""
    symbol, _ = _split_symbol(str(text or ""), split_bare=False)
    if "-" in symbol:
        base, quote = symbol.split("-", 1)
        return base, quote or None
    for quote in _SPLIT_QUOTES:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)], quote
    return symbol, None


def _alnum(text: str) -> str:
    return "".join(ch for ch in str(text).upper() if ch.isalnum())


def _aliases(base: str, quote: str) -> list[str]:
    spellings = [f"{base}{quote}", f"{base}/{quote}", f"{base}-{quote}", f"{base}/{quote}:{quote}"]
    if quote == "USDT":
        spellings.insert(0, base)
    return spellings


def resolve_symbol(query: str, *, limit: int = 20) -> dict:
    """``IdentityResolveResponse``: instruments a spelling may mean, from the
    symbol registry and what the lake stores. Venue ``listed`` is the registry
    status for the canonical venue; for other venues it means the stored series
    printed a bar in the last seven days."""
    import time as _time

    from forven.dataeng import catalog_index

    text = str(query or "").strip()
    if not text:
        return {"query": text, "candidates": []}
    base, quote = split_pair(text)
    snapshot = catalog_index.get_snapshot()
    registry = catalog_index.registry_rows(catalog_index.catalog_for(snapshot.root))
    stored: dict[str, list] = {}
    for item in snapshot.files.values():
        if item.stream != "iv":
            stored.setdefault(item.symbol, []).append(item)

    wanted: set[str] = set()
    if quote:
        wanted.add(f"{base}-{quote}")
        if quote == "USD":
            wanted.update(f"{base}-{stable}" for stable in _STABLE_QUOTES)
    for symbol in set(registry) | set(stored):
        sym_base, sym_quote = split_pair(symbol)
        if sym_base == base and (quote is None or sym_quote == quote or (quote == "USD" and sym_quote in _STABLE_QUOTES)):
            wanted.add(symbol)
        if _alnum(symbol) == _alnum(text):
            wanted.add(symbol)

    known = [symbol for symbol in wanted if symbol in registry or symbol in stored]
    if not known:
        guess = f"{base}-{quote or 'USDT'}"
        known = [guess]
    exact = f"{base}-{quote}" if quote else f"{base}-USDT"

    def rank(symbol: str) -> tuple:
        row = registry.get(symbol) or {}
        return (
            symbol != exact,
            not symbol.endswith("-USDT"),
            -(row.get("quote_volume_24h") or 0.0),
            -sum(item.rows for item in stored.get(symbol, [])),
            symbol,
        )

    now = _time.time()
    asset_memo: dict = {}
    candidates = []
    for symbol in sorted(known, key=rank)[: max(1, int(limit))]:
        row = registry.get(symbol)
        sym_base, sym_quote = split_pair(symbol)
        series = stored.get(symbol, [])
        venues = []
        canonical = [item for item in series if item.venue == "canonical"]
        if row is not None or canonical:
            stamped = [catalog_index.row_source_market(item)[1] for item in canonical if item.stream == "ohlcv"]
            first = min((item.first_ms for item in canonical if item.first_ms), default=None)
            venues.append(
                {
                    "venue": "canonical",
                    "market": str((row or {}).get("market") or (stamped[0] if stamped else "unknown")),
                    "listed": bool(row and row.get("status") == "active"),
                    "history_start": (row or {}).get("inception_ts") or catalog_index.iso_ms(first),
                }
            )
        for venue in sorted({item.venue for item in series if item.venue != "canonical"}):
            items = [item for item in series if item.venue == venue]
            last = max((item.last_ms or 0) for item in items)
            venues.append(
                {
                    "venue": venue,
                    "market": venue.partition(":")[2] or "unknown",
                    "listed": bool(last and now * 1000 - last < _RECENT_WRITE_SECONDS * 1000),
                    "history_start": catalog_index.iso_ms(min((item.first_ms for item in items if item.first_ms), default=None)),
                }
            )
        source = next((item.source for item in series if item.source), None)
        candidates.append(
            {
                "symbol": symbol,
                "display_symbol": catalog_index.display_symbol(symbol),
                "base": sym_base,
                "quote": sym_quote or "",
                "asset_class": catalog_index.asset_class_of(symbol, source, registry, asset_memo),
                "venues": venues,
                "stored": [
                    {
                        "timeframe": item.timeframe,
                        "venue": item.venue,
                        "stream": item.stream,
                        "rows": int(item.rows),
                        "last_ts": catalog_index.iso_ms(item.last_ms),
                    }
                    for item in sorted(series, key=lambda i: (i.stream != "ohlcv", i.stream, i.venue, i.timeframe))
                ],
                "delisted": bool(row and row.get("status") == "delisted"),
                "aliases": _aliases(sym_base, sym_quote) if sym_quote else [symbol],
            }
        )
    return {"query": text, "candidates": candidates}


def _dir_files(path) -> list:
    try:
        return [child for child in path.iterdir() if child.is_file()]
    except OSError:
        return []


def audit_identity() -> dict:
    """``IdentityAuditResponse``: lake folders and series whose identity does not
    fit — alias duplicates (BTCUSD, BTC-USD next to BTC-USDT), unknown symbols,
    empty or stray folders, delisted symbols still being written, and series
    written before provenance stamping. Report only: nothing is moved."""
    import time as _time
    from datetime import datetime, timezone
    from pathlib import Path

    from forven.dataeng import catalog_index

    snapshot = catalog_index.get_snapshot()
    root = Path(snapshot.root)
    registry = catalog_index.registry_rows(catalog_index.catalog_for(snapshot.root))
    frozen_symbols = {series_id.rsplit(":", 2)[-2] for series_id in catalog_index.frozen_map()}
    issues: list[dict] = []
    now = _time.time()

    def add(kind: str, path: Path, symbol: str, detail: str, suggestion: str, related=(), files=None) -> None:
        issues.append(
            {
                "kind": kind,
                "path": path.relative_to(root).as_posix(),
                "symbol": symbol,
                "detail": detail,
                "related": list(related),
                "bytes": int(sum(f.stat().st_size for f in (files if files is not None else _dir_files(path)))),
                "suggestion": suggestion,
            }
        )

    ohlcv = root / "ohlcv"
    canonical_dirs = {p.name for p in ohlcv.iterdir() if p.is_dir() and not p.name.startswith((".", "source="))} if ohlcv.is_dir() else set()

    def check_name(folder: Path, name: str, series_dirs: set[str]) -> None:
        base, quote = split_pair(name)
        if quote is None:
            add("stray_dir", folder, name, "Not a trading pair: the name has no quote currency, and nothing reads it.",
                "Review it, then move it to the trash from Storage.")
            return
        spelled = f"{base}-{quote}"
        if name != spelled:
            related = [f"{folder.parent.relative_to(root).as_posix()}/{spelled}"] if spelled in series_dirs else []
            if quote == "USD" and f"{base}-USDT" in series_dirs:
                related.append(f"{folder.parent.relative_to(root).as_posix()}/{base}-USDT")
            add("alias_duplicate", folder, name, f"Spelled without a separator; the lake's spelling of this pair is {spelled}.",
                f"Merge it into {spelled} or retire this copy.", related)
            return
        if quote == "USD" and (f"{base}-USDT" in series_dirs or f"{base}-USDT" in registry):
            add("alias_duplicate", folder, name,
                f"USD-quoted copy of {base}: Binance lists no {base}/USD market, so this was written outside the canonical "
                f"{base}-USDT series.",
                f"Compare it with {base}-USDT, then merge or retire it.",
                [f"{folder.parent.relative_to(root).as_posix()}/{base}-USDT"] if f"{base}-USDT" in series_dirs else [])
            return
        if name in registry:
            return
        if quote in _STABLE_QUOTES or base in _STABLE_QUOTES or quote not in _CRYPTO_QUOTES:
            add("unknown_symbol", folder, name, "Not in the Binance USD-M symbol registry (not a listed perp).",
                "Keep it only if a strategy needs it; otherwise retire it.")

    for name in sorted(canonical_dirs):
        folder = ohlcv / name
        files = _dir_files(folder)
        if not files and not any(folder.iterdir()):
            add("empty_dir", folder, name, "Empty folder.", "Safe to remove (Storage: empty folders).", files=[])
            continue
        if not any(f.name.endswith((".parquet", ".parquet.tail")) for f in files):
            add("stray_dir", folder, name, f"Holds no series files ({len(files)} other file(s)).",
                "Review it, then move it to the trash from Storage.", files=files)
            continue
        check_name(folder, name, canonical_dirs)
        row = registry.get(name)
        newest = max((f.stat().st_mtime for f in files), default=0.0)
        if row and row.get("status") == "delisted" and name not in frozen_symbols and now - newest < _RECENT_WRITE_SECONDS:
            written = datetime.fromtimestamp(newest, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            add("delisted_collected", folder, name, f"Delisted on Binance but still being written (last write {written}).",
                "Freeze it so the collector stops refreshing it.", files=files)

    for sources in sorted(p for p in ohlcv.glob("source=*/market=*") if p.is_dir()) if ohlcv.is_dir() else []:
        for folder in sorted(p for p in sources.iterdir() if p.is_dir()):
            if not any(folder.iterdir()):
                add("empty_dir", folder, folder.name, "Empty venue folder.", "Safe to remove (Storage: empty folders).", files=[])

    for stream_dir in _STREAM_DIRS:
        base_dir = root / stream_dir
        if not base_dir.is_dir():
            continue
        names = {p.name for p in base_dir.iterdir() if p.is_dir() and not p.name.startswith(".")}
        for name in sorted(names):
            folder = base_dir / name
            if not any(folder.iterdir()):
                add("empty_dir", folder, name, "Empty folder.", "Safe to remove (Storage: empty folders).", files=[])
                continue
            base, quote = split_pair(name)
            if quote is None or name != f"{base}-{quote}":
                check_name(folder, name, names)

    for item in snapshot.files.values():
        if item.stream == "ohlcv" and not item.source:
            add("unstamped", item.path, item.symbol,
                f"{item.timeframe} series written before provenance stamping: its source and market are unknown.",
                "Re-download it to stamp its source, or leave it as is.", files=list(item.paths))

    order = {"alias_duplicate": 0, "delisted_collected": 1, "unknown_symbol": 2, "stray_dir": 3, "unstamped": 4, "empty_dir": 5}
    issues.sort(key=lambda issue: (order.get(issue["kind"], 9), issue["path"]))
    return {"generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "issues": issues}
