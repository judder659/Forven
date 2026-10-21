"""Resolve the ``dataset_id`` a backtest request names to ``(symbol, timeframe)``.

Agents have passed ``"1"``, ``"dataset-299"``, ``"BINANCEUSDM/USDT"`` and
``"BINANCE:AVAXUSDT/USDT"`` as dataset ids. The shared parser left ``1`` or
the exchange name as the symbol, the data fetch failed, and every attempt was
recorded as a failed Simulation Agent task. :func:`resolve_backtest_dataset`
settles the id before any task row exists:

* a bare or numbered id (``"1"``, ``"dataset-299"``) is looked up in the
  dataset catalog (the ``forven_list_datasets`` listing, whose ids are
  ``dataset-<n>-<SYMBOL>-<tf>``) and resolves to that dataset's symbol and
  timeframe, or is refused when no such dataset exists;
* anything else must be a market symbol (:func:`forven.symbols.normalize_symbol`)
  that names a market rather than an exchange or placeholder
  (:func:`forven.creation_markets.non_market_reason`). A symbol the catalog
  stores is always accepted, so non-crypto series keep working.

The catalog lives in ``forven.api_domains.data``, which is in the first-party
import cycle with ``forven.api_core``; it is read through importlib.
"""

from __future__ import annotations

import importlib
import logging
import re
from dataclasses import dataclass
from typing import Any

from forven.creation_markets import non_market_reason
from forven.symbols import normalize_symbol, parse_dataset_id

log = logging.getLogger("forven.backtest_dataset_ids")

LIST_HINT = " Use forven_list_datasets for valid ids."

_NUMBERED_ID_RE = re.compile(r"^(?:dataset-)?(\d+)$", re.IGNORECASE)


@dataclass(frozen=True)
class DatasetResolution:
    """``symbol``/``timeframe`` when ``error`` is None; otherwise a readable refusal."""

    symbol: str = ""
    timeframe: str | None = None
    error: str | None = None


def _catalog() -> list[dict[str, Any]]:
    try:
        rows = importlib.import_module("forven.api_domains.data").get_datasets_stub()
    except Exception as exc:
        log.warning("backtest dataset id: catalog read failed: %s", exc)
        return []
    return [dict(row) for row in rows or [] if isinstance(row, dict)]


def _catalog_row_for_number(number: int, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    exact = {str(number), f"dataset-{number}"}
    prefix = f"dataset-{number}-"
    for row in rows:
        row_id = str(row.get("id") or "").strip().lower()
        if row_id in exact or row_id.startswith(prefix):
            return row
    return None


def _symbol_key(symbol: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(symbol or "").upper())


def _catalog_stores(symbol: str, rows: list[dict[str, Any]]) -> bool:
    key = _symbol_key(symbol)
    return bool(key) and any(_symbol_key(row.get("symbol")) == key for row in rows)


def resolve_backtest_dataset(dataset_id: str) -> DatasetResolution:
    """Turn a backtest request's ``dataset_id`` into a symbol and timeframe.

    ``"BTC/USDT-1h"`` -> ``BTC/USDT``, ``1h`` (as :func:`parse_dataset_id`);
    ``"BTC/USDT 1h"`` (legacy space form) -> ``BTC/USDT``, ``1h``;
    ``"1"`` / ``"dataset-1"`` -> the catalog dataset numbered 1; anything that
    is not a market symbol -> ``error``. The timeframe is None when the id
    names none, so the strategy row's own timeframe can apply.
    """
    text = str(dataset_id or "").strip()
    numbered = _NUMBERED_ID_RE.match(text)
    if numbered:
        row = _catalog_row_for_number(int(numbered.group(1)), _catalog())
        symbol = str((row or {}).get("symbol") or "").strip()
        timeframe = str((row or {}).get("timeframe") or "").strip() or None
        if not symbol:
            return DatasetResolution(
                error=f"dataset_id '{text}' does not match a stored dataset." + LIST_HINT
            )
        return DatasetResolution(symbol=symbol, timeframe=timeframe)

    if " " in text:
        # Legacy space-separated format: "BTC/USDT 1h".
        symbol, timeframe = text.split()[0], text.split()[-1]
    else:
        # Strips any "dataset-26-" / "DATASET-" prefix and "-ccxt" source
        # suffix and splits off the timeframe ("BTC/USDT" names none).
        symbol, timeframe = parse_dataset_id(text)

    check = normalize_symbol(symbol)
    reason = check.reason if not check.ok else non_market_reason(symbol)
    if reason:
        if _catalog_stores(symbol, _catalog()):
            return DatasetResolution(symbol=symbol, timeframe=timeframe)
        return DatasetResolution(
            error=f"dataset_id '{text}': {reason.rstrip('.')}." + LIST_HINT
        )
    return DatasetResolution(symbol=symbol, timeframe=timeframe)
