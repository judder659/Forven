"""One shared market-symbol normaliser.

Strategies were registered and tasks dispatched with symbols such as
``1/USDT`` and ``DATASET-SUI/USDT``. Each normaliser in the codebase accepted
any base token: a leftover dataset prefix stayed glued to the base, and a bare
``1`` (from a split ``dataset-1-...`` id or a ``1h`` timeframe) was paired with
USDT. Those symbols then failed later, at the data check before a backtest,
with an error that did not say what was wrong.

This module is the one place that decides what a market symbol looks like:

* :func:`parse_dataset_id` turns a dataset id (``dataset-3-ETH/USDT-4h``,
  ``SUI/USDT-1h-ccxt``) into ``(symbol, timeframe)``.
* :func:`normalize_symbol` returns a :class:`SymbolCheck` with the canonical
  ``BASE/QUOTE`` form, or a readable reason it is not a market symbol.

It is a leaf (standard library only) so any layer can import it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

KNOWN_QUOTES: tuple[str, ...] = ("USDT", "USDC", "USD", "BUSD", "BTC", "ETH", "DAI", "FDUSD")
# Quotes recognised when glued to the base (``ETHUSDT``). BTC/ETH are left out:
# ``WBTC`` or ``STETH`` must not split into a base and a quote.
_CONCAT_QUOTES: tuple[str, ...] = ("USDT", "USDC", "FDUSD", "BUSD", "USD")

_DATASET_PREFIX_RE = re.compile(r"^(?:remote-)?dataset(?:-\d+)?-", re.IGNORECASE)
_SOURCE_SUFFIX_RE = re.compile(r"[-_](?:ccxt|binance|hyperliquid|bybit|okx)$", re.IGNORECASE)
_TIMEFRAME_TOKEN = r"(?:1|3|5|15|30)m|(?:1|2|4|6|8|12)h|(?:1|3)d|1w|1mo"
_TIMEFRAME_RE = re.compile(rf"^(?:{_TIMEFRAME_TOKEN})$", re.IGNORECASE)
_TIMEFRAME_SUFFIX_RE = re.compile(rf"[-_/]({_TIMEFRAME_TOKEN})$", re.IGNORECASE)
_PERP_SUFFIX_RE = re.compile(r"[-_]PERP$", re.IGNORECASE)
# A base is 2-16 letters/digits with at least one letter (``1INCH`` and
# ``1000PEPE`` are real; ``1`` is not), and is never a timeframe token.
_BASE_RE = re.compile(r"^(?=.*[A-Z])[A-Z0-9]{2,16}$")
_PLACEHOLDER_BASES = frozenset({"DATASET", "GENERIC", "UNKNOWN", "UNSPECIFIED", "TBD", "NONE", "NULL"})


@dataclass(frozen=True)
class SymbolCheck:
    """Result of :func:`normalize_symbol`.

    ``symbol`` is the canonical ``BASE/QUOTE`` when ``ok``; otherwise the
    cleaned input, for messages. ``repaired`` is True when anything beyond
    case and separator was removed (a dataset prefix, timeframe or source
    suffix); ``timeframe`` is the timeframe found in the input, if any.
    """

    ok: bool
    symbol: str
    reason: str | None = None
    repaired: bool = False
    timeframe: str | None = None


def parse_dataset_id(value: str) -> tuple[str, str | None]:
    """Split a dataset id or context name into ``(symbol part, timeframe)``.

    ``dataset-3-ETH/USDT-4h`` -> ``("ETH/USDT", "4h")``;
    ``DATASET-SUI/USDT`` -> ``("SUI/USDT", None)``;
    ``SUI/USDT-1h-ccxt`` -> ``("SUI/USDT", "1h")``. The symbol part is not
    validated; pass it to :func:`normalize_symbol` for that.
    """
    text = str(value or "").strip()
    text = _DATASET_PREFIX_RE.sub("", text)
    text = _SOURCE_SUFFIX_RE.sub("", text)
    timeframe = None
    match = _TIMEFRAME_SUFFIX_RE.search(text)
    if match:
        timeframe = match.group(1).lower()
        text = text[: match.start()]
    text = _SOURCE_SUFFIX_RE.sub("", text)
    return text, timeframe


def _base_problem(base: str) -> str | None:
    if _TIMEFRAME_RE.match(base):
        return f"'{base}' is a timeframe, not a coin"
    if base in _PLACEHOLDER_BASES:
        return f"'{base}' is a placeholder, not a coin"
    if not _BASE_RE.match(base):
        return f"'{base}' is not a coin ticker (2-16 letters or digits, at least one letter)"
    return None


def normalize_symbol(value: str | None, *, default_quote: str = "USDT") -> SymbolCheck:
    """Canonicalise a market symbol to ``BASE/QUOTE`` or say why it is not one.

    Accepts ``ETH``, ``eth/usdt``, ``ETH-USDT``, ``ETHUSDT``, ``ETH/USDT:USDT``,
    ``ETH-PERP`` and dataset ids such as ``dataset-3-ETH/USDT-4h`` (repaired,
    with the timeframe reported). Rejects ``1``, ``1H``, ``DATASET``, unknown
    quotes and anything with more than one separator.
    """
    raw = str(value or "").strip()
    if not raw:
        return SymbolCheck(False, "", "empty symbol")
    text, timeframe = parse_dataset_id(raw)
    text = _PERP_SUFFIX_RE.sub("", text.strip().upper())
    if ":" in text and "/" in text.split(":", 1)[0]:
        # ccxt perp form ``ETH/USDT:USDT``: the settle currency is not part of the pair.
        text = text.split(":", 1)[0]
    repaired = (
        timeframe is not None
        or _DATASET_PREFIX_RE.match(raw) is not None
        or _SOURCE_SUFFIX_RE.search(raw) is not None
    )
    hint = ""
    if timeframe:
        hint = f" (it looks like a dataset id; use symbol '{text}' and timeframe '{timeframe}')"
    if not text:
        return SymbolCheck(False, "", f"'{raw}' has no market symbol{hint}", repaired, timeframe)
    if not re.fullmatch(r"[A-Z0-9/\-_]+", text):
        return SymbolCheck(False, text, f"'{raw}' contains characters a market symbol cannot have", repaired, timeframe)

    for sep in ("/", "-", "_"):
        if sep in text:
            parts = text.split(sep)
            if len(parts) != 2 or not all(parts):
                return SymbolCheck(
                    False, text,
                    f"'{raw}' is not BASE/QUOTE: a market symbol has exactly one separator{hint}",
                    repaired, timeframe,
                )
            base, quote = parts
            if quote not in KNOWN_QUOTES:
                return SymbolCheck(
                    False, text,
                    f"'{raw}' has unknown quote currency '{quote}' (expected one of {', '.join(KNOWN_QUOTES)})",
                    repaired, timeframe,
                )
            problem = _base_problem(base)
            if problem:
                return SymbolCheck(False, text, f"'{raw}' is not a market symbol: {problem}{hint}", repaired, timeframe)
            return SymbolCheck(True, f"{base}/{quote}", None, repaired, timeframe)

    for quote in _CONCAT_QUOTES:
        if text.endswith(quote) and len(text) > len(quote) + 1:
            base = text[: -len(quote)]
            if _base_problem(base) is None:
                return SymbolCheck(True, f"{base}/{quote}", None, repaired, timeframe)

    problem = _base_problem(text)
    if problem:
        return SymbolCheck(False, text, f"'{raw}' is not a market symbol: {problem}{hint}", repaired, timeframe)
    return SymbolCheck(True, f"{text}/{default_quote}", None, repaired, timeframe)
