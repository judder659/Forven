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


# ---------------------------------------------------------------- spellings
#
# One instrument can be spelled many ways (BTC, BTCUSDT, BTC/USDT, BTC-USDT,
# BTC/USDT:USDT); ``split_pair`` reads any of them. Resolving a spelling to
# stored series and auditing the lake live in ``identity_audit``: this module
# is the low-level parser everything imports, so it must not import the catalog.

# Quote suffixes a bare spelling (BTCUSDT) is split on. Fiat codes such as TRY
# are left out: "RETRY" is not RE/TRY.
_SPLIT_QUOTES = ("FDUSD", "USDT", "USDC", "BUSD", "USD", "BTC", "ETH", "BNB")


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
