import logging
import os
import threading
import time
import urllib.parse
from datetime import datetime, timezone

import pandas as pd
from pathlib import Path

import httpx
from fastapi import HTTPException

from forven import api_core as core
from forven.db import _now
from forven.market_data import fetch_hyperliquid_candles

log = logging.getLogger("forven.api")

_REMOTE_DATA_ROOT_ENV = "FORVEN_REMOTE_ENGINE_DATA_ROOT"
_REMOTE_DATA_ALLOWED_ROOT_ENV = "FORVEN_REMOTE_ENGINE_ALLOWED_ROOT"


def _remote_data_engine_config() -> tuple[bool, str]:
    settings = core._load_settings_payload()
    enabled = bool(settings.get("remote_engine_enabled"))
    url = str(settings.get("remote_engine_url") or "").strip().rstrip("/")
    return enabled, url


def _resolve_remote_data_root() -> str | None:
    env_path = str(os.getenv(_REMOTE_DATA_ROOT_ENV, "") or "").strip()
    if env_path:
        return env_path
    settings = core._load_settings_payload()
    configured = str(settings.get("remote_engine_data_root") or "").strip()
    return configured or None


def _resolve_remote_data_allowed_root() -> str | None:
    env_path = str(os.getenv(_REMOTE_DATA_ALLOWED_ROOT_ENV, "") or "").strip()
    if env_path:
        return env_path
    settings = core._load_settings_payload()
    configured = str(settings.get("remote_engine_allowed_root") or "").strip()
    return configured or None


def _contains_parent_traversal(path_value: str) -> bool:
    normalized = str(path_value or "").replace("\\", "/")
    return any(part == ".." for part in normalized.split("/"))


def _remote_root_candidates(raw: str) -> list[str]:
    candidates: list[str] = [raw]
    if "\\" in raw:
        candidates.append(raw.replace("\\", "/"))
    if raw.startswith("\\\\"):
        candidates.append("//" + raw.lstrip("\\").replace("\\", "/"))

    deduped: list[str] = []
    for candidate in candidates:
        if candidate not in deduped:
            deduped.append(candidate)
    return deduped


def _resolve_existing_remote_data_root_path(remote_root: str) -> Path:
    raw = str(remote_root or "").strip()
    if not raw:
        raise HTTPException(status_code=503, detail="remote data root path is empty")
    if _contains_parent_traversal(raw):
        raise HTTPException(status_code=400, detail="remote data root path cannot contain '..' traversal segments")

    allowed_root_raw = _resolve_remote_data_allowed_root()
    allowed_root: Path | None = None
    if allowed_root_raw:
        if _contains_parent_traversal(allowed_root_raw):
            raise HTTPException(status_code=400, detail="remote allowed root cannot contain '..' traversal segments")
        try:
            allowed_root = Path(allowed_root_raw).expanduser().resolve(strict=True)
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"remote allowed root is not reachable: {allowed_root_raw}",
            ) from exc

    for candidate in _remote_root_candidates(raw):
        try:
            path = Path(candidate).expanduser().resolve(strict=True)
        except OSError:
            continue
        if allowed_root is not None:
            try:
                path.relative_to(allowed_root)
            except ValueError as exc:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"remote data root path escapes allowed root: "
                        f"{path} is outside {allowed_root}"
                    ),
                ) from exc
        if not path.is_dir():
            raise HTTPException(status_code=503, detail=f"remote data root path is not a directory: {path}")
        return path

    raise HTTPException(
        status_code=503,
        detail=f"remote data root path is not reachable: {raw}",
    )


def _remote_endpoint_candidates(remote_url: str, api_path: str, alt_path: str | None = None) -> list[str]:
    base = str(remote_url or "").strip().rstrip("/")
    if not base:
        return []

    normalized_api_path = api_path if str(api_path).startswith("/") else f"/{api_path}"
    candidates: list[str] = []
    if base.endswith("/api"):
        candidates.append(f"{base}{normalized_api_path}")
    else:
        candidates.append(f"{base}/api{normalized_api_path}")

    if alt_path:
        normalized_alt = alt_path if str(alt_path).startswith("/") else f"/{alt_path}"
        candidates.append(f"{base}{normalized_alt}")

    deduped: list[str] = []
    for candidate in candidates:
        if candidate not in deduped:
            deduped.append(candidate)
    return deduped


def _extract_remote_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = payload.get("detail") or payload.get("message") or payload.get("error")
            if isinstance(detail, str) and detail.strip():
                return detail.strip()
    except Exception:
        pass
    text = str(response.text or "").strip()
    return text[:300] if text else f"HTTP {response.status_code}"


def _request_remote_json(
    method: str,
    candidates: list[str],
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    timeout: float = 12.0,
) -> tuple[object, str]:
    if not candidates:
        raise HTTPException(
            status_code=503,
            detail="Remote data source is enabled but no remote_engine_url is configured.",
        )

    request_params = dict(params or {})
    request_params.setdefault("remote_skip", "1")
    last_error = "no compatible remote data endpoint found"
    for target in candidates:
        try:
            response = httpx.request(
                method.upper(),
                target,
                params=request_params,
                json=json_body,
                timeout=timeout,
            )
        except Exception as exc:
            last_error = f"{target}: {exc}"
            continue

        if response.status_code == 404:
            last_error = f"{target}: HTTP 404"
            continue

        if response.status_code >= 400:
            detail = _extract_remote_error_detail(response)
            raise HTTPException(
                status_code=503,
                detail=f"Remote data request failed ({target}): {detail}",
            )

        try:
            return response.json(), target
        except Exception as exc:
            log.exception("Remote data endpoint returned invalid JSON: %s", target)
            raise HTTPException(
                status_code=502,
                detail=f"Remote data endpoint returned invalid JSON ({target}): {exc}",
            ) from exc

    raise HTTPException(
        status_code=503,
        detail=f"Remote data source is enabled but unavailable: {last_error}",
    )


def _coerce_remote_rows(
    payload: object,
    *,
    collection_name: str,
    endpoint_url: str,
) -> list[dict]:
    rows: object = payload
    if isinstance(payload, dict):
        for key in (collection_name, "items", "data", "results", "runs", "datasets"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
    if not isinstance(rows, list):
        raise HTTPException(
            status_code=502,
            detail=(
                f"Remote data endpoint returned unsupported payload shape "
                f"({endpoint_url}); expected an array or object containing '{collection_name}'."
            ),
        )
    normalized_rows: list[dict] = []
    for row in rows:
        if isinstance(row, dict):
            normalized_rows.append(dict(row))
    return normalized_rows


def _to_ui_symbol(symbol: object) -> str:
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    if "/" in raw:
        return raw
    if "-" in raw:
        base, quote = raw.split("-", 1)
        if base and quote:
            return f"{base}/{quote}"
    return raw


def _normalize_dataset_rows(raw_rows: list[dict]) -> list[dict[str, object]]:
    from forven.data import classify_dataset_asset_class, dataset_market_type

    rows: list[dict[str, object]] = []
    for idx, raw in enumerate(raw_rows):
        symbol = _to_ui_symbol(raw.get("symbol"))
        timeframe = str(raw.get("timeframe") or "").strip()
        if not symbol or not timeframe:
            continue
        source = str(raw.get("source") or "local").strip() or "local"
        asset_class = str(raw.get("asset_class") or "").strip().lower()
        if not asset_class:
            asset_class = classify_dataset_asset_class(symbol, source)
        market_type = str(raw.get("market_type") or "").strip().lower()
        if not market_type:
            market_type = dataset_market_type(asset_class)
        try:
            row_count = int(raw.get("row_count") or 0)
        except Exception:
            row_count = 0
        start_ts = str(raw.get("start_ts") or "")
        end_ts = str(raw.get("end_ts") or "")
        dataset_id = raw.get("id")
        if not dataset_id:
            dataset_id = f"dataset-{idx}-{symbol}-{timeframe}"
        rows.append(
            {
                "id": dataset_id,
                "symbol": symbol,
                "timeframe": timeframe,
                "source": source,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "row_count": row_count,
                "asset_class": asset_class,
                "market_type": market_type,
                # Venue identity from the forven_market parquet stamp
                # (perp/spot/unknown; "unstamped" = legacy pre-stamping file).
                "market": str(raw.get("market") or "unstamped").strip().lower(),
            }
        )
    rows.sort(
        key=lambda row: core._to_datetime_sort_key(row.get("end_ts") or row.get("start_ts")),
        reverse=True,
    )
    return rows


def _scan_remote_data_root_datasets(remote_root: str) -> list[dict[str, object]]:
    from forven.data import _dataset_from_file

    root = _resolve_existing_remote_data_root_path(remote_root)
    raw_rows: list[dict] = []
    for symbol_dir in sorted(root.iterdir()):
        if not symbol_dir.is_dir():
            continue
        symbol = symbol_dir.name
        for parquet_file in sorted(symbol_dir.glob("*.parquet")):
            timeframe = parquet_file.stem
            try:
                row = _dataset_from_file(parquet_file, symbol, timeframe)
            except Exception:
                continue
            if isinstance(row, dict):
                raw_rows.append(row)

    if not raw_rows:
        raise HTTPException(
            status_code=503,
            detail=f"no parquet datasets found in remote data root: {remote_root}",
        )
    return _normalize_dataset_rows(raw_rows)


def _dataset_rows_to_remote_runs(
    datasets: list[dict[str, object]],
    *,
    symbol: str | None,
    status: str | None,
    limit: int,
    offset: int,
) -> list[dict]:
    normalized_symbol = _to_ui_symbol(symbol) if symbol else None
    normalized_status = str(status or "").strip().lower() if status else None

    runs: list[dict] = []
    for idx, ds in enumerate(datasets):
        ds_symbol = str(ds.get("symbol") or "").strip()
        ds_tf = str(ds.get("timeframe") or "").strip()
        if not ds_symbol or not ds_tf:
            continue
        if normalized_symbol and ds_symbol != normalized_symbol:
            continue
        if normalized_status and normalized_status != "completed":
            continue
        completed_at = str(ds.get("end_ts") or ds.get("start_ts") or _now())
        bars = int(ds.get("row_count") or 0)
        runs.append(
            {
                "id": f"remote-dataset-{idx}-{ds_symbol}-{ds_tf}",
                "symbol": ds_symbol,
                "timeframe": ds_tf,
                "source": str(ds.get("source") or "remote_share"),
                "status": "completed",
                "bars_fetched": bars,
                "bars_new": bars,
                "error": None,
                "started_at": str(ds.get("start_ts") or completed_at),
                "completed_at": completed_at,
            }
        )

    runs.sort(
        key=lambda item: core._to_datetime_sort_key(item.get("completed_at") or item.get("started_at")),
        reverse=True,
    )
    start_index = max(int(offset), 0)
    end_index = start_index + max(int(limit), 1)
    return runs[start_index:end_index]


def _fetch_remote_datasets(remote_url: str) -> list[dict[str, object]]:
    try:
        payload, endpoint = _request_remote_json(
            "GET",
            _remote_endpoint_candidates(remote_url, "/datasets", "/data/datasets"),
            timeout=10.0,
        )
        rows = _coerce_remote_rows(payload, collection_name="datasets", endpoint_url=endpoint)
        return _normalize_dataset_rows(rows)
    except HTTPException as api_exc:
        remote_root = _resolve_remote_data_root()
        if not remote_root:
            raise
        try:
            return _scan_remote_data_root_datasets(remote_root)
        except HTTPException as root_exc:
            raise HTTPException(
                status_code=503,
                detail=f"{api_exc.detail} | remote data root scan failed: {root_exc.detail}",
            ) from root_exc


def _fetch_remote_ingestion_runs(
    remote_url: str,
    *,
    symbol: str | None,
    status: str | None,
    limit: int,
    offset: int,
) -> list[dict]:
    query_params = {
        "limit": max(int(limit), 1),
        "offset": max(int(offset), 0),
    }
    if symbol:
        query_params["symbol"] = symbol
    if status:
        query_params["status"] = status

    try:
        payload, endpoint = _request_remote_json(
            "GET",
            _remote_endpoint_candidates(remote_url, "/data/ingestion/runs", "/data/ingestion/runs"),
            params=query_params,
            timeout=10.0,
        )
        rows = _coerce_remote_rows(payload, collection_name="runs", endpoint_url=endpoint)
        normalized_rows: list[dict] = []
        for row in rows:
            current = dict(row)
            current["symbol"] = _to_ui_symbol(current.get("symbol"))
            normalized_rows.append(current)
        normalized_rows.sort(
            key=lambda item: core._to_datetime_sort_key(item.get("completed_at") or item.get("started_at")),
            reverse=True,
        )
        return normalized_rows
    except HTTPException as api_exc:
        remote_root = _resolve_remote_data_root()
        if not remote_root:
            raise
        try:
            datasets = _scan_remote_data_root_datasets(remote_root)
            return _dataset_rows_to_remote_runs(
                datasets,
                symbol=symbol,
                status=status,
                limit=limit,
                offset=offset,
            )
        except HTTPException as root_exc:
            raise HTTPException(
                status_code=503,
                detail=f"{api_exc.detail} | remote data root scan failed: {root_exc.detail}",
            ) from root_exc


def get_data_ingestion_runs(
    symbol: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    remote_skip: bool = False,
):
    from forven.data import get_active_ingestion_runs, scan_datasets

    remote_enabled, remote_url = _remote_data_engine_config()
    if remote_enabled and not remote_skip:
        return _fetch_remote_ingestion_runs(
            remote_url,
            symbol=symbol,
            status=status,
            limit=limit,
            offset=offset,
        )

    active = get_active_ingestion_runs()
    datasets = scan_datasets()
    legacy = []

    for idx, ds in enumerate(datasets):
        completed_at = ds.get("end_ts") or ds.get("start_ts")
        dataset_symbol = _to_ui_symbol(ds.get("symbol"))
        legacy.append(
            {
                "id": f"dataset-{idx}-{dataset_symbol}-{ds['timeframe']}",
                "symbol": dataset_symbol,
                "timeframe": ds["timeframe"],
                "source": ds.get("source", "local"),
                "status": "completed",
                "bars_fetched": ds.get("row_count", 0),
                "bars_new": ds.get("row_count", 0),
                "error": None,
                "started_at": completed_at or datetime.now(timezone.utc).isoformat(),
                "completed_at": completed_at,
            }
        )

    normalized_active: list[dict[str, object]] = []
    for run in active:
        if not isinstance(run, dict):
            continue
        current = dict(run)
        current["symbol"] = _to_ui_symbol(current.get("symbol"))
        normalized_active.append(current)

    combined = normalized_active + legacy
    if symbol:
        combined = [row for row in combined if row["symbol"] == symbol]
    if status:
        combined = [row for row in combined if row["status"] == status]

    def _sort_key(row):
        value = row.get("completed_at") or row.get("started_at")
        return core._to_datetime_sort_key(value)

    combined.sort(key=_sort_key, reverse=True)
    return combined[offset : offset + limit]


def get_cached_data_ingestion_runs(
    symbol: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    from forven.data import get_active_ingestion_runs, peek_cached_datasets

    active = get_active_ingestion_runs()
    datasets = peek_cached_datasets()
    legacy = []

    for idx, ds in enumerate(datasets):
        completed_at = ds.get("end_ts") or ds.get("start_ts")
        dataset_symbol = _to_ui_symbol(ds.get("symbol"))
        legacy.append(
            {
                "id": f"dataset-{idx}-{dataset_symbol}-{ds['timeframe']}",
                "symbol": dataset_symbol,
                "timeframe": ds["timeframe"],
                "source": ds.get("source", "local"),
                "status": "completed",
                "bars_fetched": ds.get("row_count", 0),
                "bars_new": ds.get("row_count", 0),
                "error": None,
                "started_at": completed_at or datetime.now(timezone.utc).isoformat(),
                "completed_at": completed_at,
            }
        )

    normalized_active: list[dict[str, object]] = []
    for run in active:
        if not isinstance(run, dict):
            continue
        current = dict(run)
        current["symbol"] = _to_ui_symbol(current.get("symbol"))
        normalized_active.append(current)

    combined = normalized_active + legacy
    if symbol:
        combined = [row for row in combined if row["symbol"] == symbol]
    if status:
        combined = [row for row in combined if row["status"] == status]

    def _sort_key(row):
        value = row.get("completed_at") or row.get("started_at")
        return core._to_datetime_sort_key(value)

    combined.sort(key=_sort_key, reverse=True)
    return combined[offset : offset + limit]


def post_data_ingestion_submit(
    symbol: str,
    timeframe: str,
    exchange: str = "binance",
    limit: int = 1000,
    since: int | None = None,
    until: int | None = None,
    all_available: bool = False,
    remote_skip: bool = False,
):
    remote_enabled, remote_url = _remote_data_engine_config()
    if remote_enabled and not remote_skip:
        log.info("Delegating data ingestion to remote data lake: %s %s", symbol, timeframe)
        if not remote_url:
            raise HTTPException(
                status_code=503,
                detail="Remote Data Mode is enabled but remote_engine_url is empty.",
            )
        url = remote_url.rstrip("/") + "/data/ingest"
        payload = {
            "symbol": symbol,
            "timeframe": timeframe,
            "exchange": exchange,
            "limit": limit,
            "since_ms": since,
            "until_ms": until,
            "all_available": all_available,
        }
        try:
            resp = httpx.post(url, json=payload, timeout=20.0)
            resp.raise_for_status()
            data = resp.json()

            run = {
                "id": data.get("run_id"),
                "symbol": symbol,
                "timeframe": timeframe,
                "source": "remote_lake",
                "status": data.get("status", "completed"),
                "bars_fetched": limit if limit else 50000,
                "bars_new": limit if limit else 50000,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "error": None,
            }
            from forven.data import _ingestion_runs, _ingestion_runs_lock

            with _ingestion_runs_lock:
                _ingestion_runs[data.get("run_id")] = run
            return run
        except Exception as exc:
            log.warning("Remote data ingestion failed: %s", exc)
            raise HTTPException(
                status_code=503,
                detail=f"Remote data ingestion failed: {exc}",
            ) from exc

    from forven.data import submit_ingestion

    return submit_ingestion(
        symbol=symbol,
        timeframe=timeframe,
        exchange=exchange,
        limit=limit if not all_available else None,
        since_ms=since,
        until_ms=until,
        all_available=all_available,
    )


def get_data_ingestion_run(run_id: str):
    # Live runs (and remote-mode fabricated runs) are keyed in the in-memory
    # run store — a direct lookup, not the previous linear scan of up to 10k
    # reconstructed rows on every 1.5s frontend poll.
    from forven.data import get_ingestion_run

    run = get_ingestion_run(str(run_id))
    if run is not None:
        run["symbol"] = _to_ui_symbol(run.get("symbol"))
        return run
    # Synthetic catalog ids ("dataset-N-...") and remote-listed runs still go
    # through the composite listing.
    rows = get_data_ingestion_runs(limit=10_000, offset=0)
    match = next((row for row in rows if str(row.get("id")) == str(run_id)), None)
    if match is None:
        raise HTTPException(status_code=404, detail=f"ingestion run not found: {run_id}")
    return match


def post_fetch_data(
    symbol: str,
    timeframe: str,
    exchange: str = "binance",
    limit: int = 1000,
    since: int | None = None,
    until: int | None = None,
    all_available: bool = False,
    remote_skip: bool = False,
):
    remote_enabled, remote_url = _remote_data_engine_config()
    if remote_enabled and not remote_skip:
        log.info("Delegating direct data fetch to remote data lake: %s %s", symbol, timeframe)
        if not remote_url:
            raise HTTPException(
                status_code=503,
                detail="Remote Data Mode is enabled but remote_engine_url is empty.",
            )
        url = remote_url.rstrip("/") + "/data/ingest"
        payload = {
            "symbol": symbol,
            "timeframe": timeframe,
            "exchange": exchange,
            "limit": limit,
            "since_ms": since,
            "until_ms": until,
            "all_available": all_available,
        }
        try:
            resp = httpx.post(url, json=payload, timeout=20.0)
            resp.raise_for_status()
            return {
                "symbol": _to_ui_symbol(symbol),
                "timeframe": timeframe,
                "source": exchange,
                "start_ts": "2015-01-01T00:00:00Z",
                "end_ts": datetime.now().isoformat() + "Z",
                "row_count": limit if limit else 50000,
                "bars_fetched": limit if limit else 50000,
                "bars_new": limit if limit else 50000,
            }
        except Exception as exc:
            log.warning("Remote direct data fetch failed: %s", exc)
            raise HTTPException(
                status_code=503,
                detail=f"Remote direct data fetch failed: {exc}",
            ) from exc

    try:
        from forven.data import fetch_ohlcv_chunked

        payload = fetch_ohlcv_chunked(
            symbol=symbol,
            timeframe=timeframe,
            exchange_id=exchange,
            limit=limit if not all_available else None,
            since_ms=since,
            until_ms=until,
            all_available=all_available,
        )
        if isinstance(payload, dict):
            payload = dict(payload)
            payload["symbol"] = _to_ui_symbol(payload.get("symbol"))
        return payload
    except Exception as exc:
        log.error("Failed to fetch data for %s: %s", symbol, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def get_datasets_stub(remote_skip: bool = False):
    remote_enabled, remote_url = _remote_data_engine_config()
    if remote_enabled and not remote_skip:
        return _fetch_remote_datasets(remote_url)

    try:
        from forven.data import scan_datasets

        raw_rows = scan_datasets()
    except Exception as exc:
        log.warning("Failed to scan datasets for catalog endpoint: %s", exc)
        return []

    normalized = [dict(row) for row in raw_rows if isinstance(row, dict)]
    return _normalize_dataset_rows(normalized)


def get_cached_datasets_stub() -> list[dict[str, object]]:
    try:
        from forven.data import peek_cached_datasets

        raw_rows = peek_cached_datasets()
    except Exception as exc:
        log.debug("Failed to read cached dataset catalog snapshot: %s", exc)
        return []

    normalized = [dict(row) for row in raw_rows if isinstance(row, dict)]
    return _normalize_dataset_rows(normalized)


def get_dataset_detail_stub(symbol: str, timeframe: str, remote_skip: bool = False):
    remote_enabled, remote_url = _remote_data_engine_config()
    if remote_enabled and not remote_skip:
        encoded_symbol = urllib.parse.quote(str(symbol or "").strip(), safe="")
        encoded_tf = urllib.parse.quote(str(timeframe or "").strip(), safe="")
        payload, _ = _request_remote_json(
            "GET",
            _remote_endpoint_candidates(remote_url, f"/datasets/{encoded_symbol}/{encoded_tf}"),
            timeout=10.0,
        )
        if isinstance(payload, dict):
            current = dict(payload)
            current["symbol"] = _to_ui_symbol(current.get("symbol"))
            return current
        raise HTTPException(
            status_code=502,
            detail="Remote dataset detail endpoint returned invalid payload.",
        )

    from forven.data import get_dataset_detail

    try:
        payload = get_dataset_detail(symbol, timeframe)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        log.error("Failed to read dataset detail for %s %s: %s", symbol, timeframe, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if isinstance(payload, dict):
        payload = dict(payload)
        payload["symbol"] = _to_ui_symbol(payload.get("symbol"))
    return payload


def delete_dataset_stub(symbol: str, timeframe: str, remote_skip: bool = False):
    remote_enabled, remote_url = _remote_data_engine_config()
    if remote_enabled and not remote_skip:
        encoded_symbol = urllib.parse.quote(str(symbol or "").strip(), safe="")
        encoded_tf = urllib.parse.quote(str(timeframe or "").strip(), safe="")
        payload, _ = _request_remote_json(
            "DELETE",
            _remote_endpoint_candidates(remote_url, f"/datasets/{encoded_symbol}/{encoded_tf}"),
            timeout=10.0,
        )
        if isinstance(payload, dict):
            return payload
        return {
            "status": "deleted",
            "symbol": _to_ui_symbol(symbol),
            "timeframe": timeframe,
        }

    from forven.data import delete_dataset

    try:
        deleted = bool(delete_dataset(symbol, timeframe))
    except Exception as exc:
        log.error("Failed to delete dataset %s %s: %s", symbol, timeframe, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail=f"dataset not found: {symbol} {timeframe}")
    return {
        "status": "deleted",
        "symbol": _to_ui_symbol(symbol),
        "timeframe": timeframe,
    }


def get_data_quality(symbol: str, timeframe: str, remote_skip: bool = False):
    remote_enabled, remote_url = _remote_data_engine_config()
    if remote_enabled and not remote_skip:
        payload, _ = _request_remote_json(
            "GET",
            _remote_endpoint_candidates(remote_url, "/data/quality", "/data/quality"),
            params={"symbol": symbol, "timeframe": timeframe},
            timeout=10.0,
        )
        if isinstance(payload, dict):
            current = dict(payload)
            current["symbol"] = _to_ui_symbol(current.get("symbol"))
            return current
        raise HTTPException(
            status_code=502,
            detail="Remote data quality endpoint returned invalid payload.",
        )

    from forven.data import compute_data_quality

    try:
        payload = compute_data_quality(symbol, timeframe)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.error("Failed to compute data quality for %s %s: %s", symbol, timeframe, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if isinstance(payload, dict):
        payload = dict(payload)
        payload["symbol"] = _to_ui_symbol(payload.get("symbol"))
    return payload


# Data-quality leaderboard. The frontend used to have NO backend route for this
# (it called /data/quality/reports, got a 404, and fell back to firing up to
# ~100 CONCURRENT /api/data/quality requests — one full parquet scan each). That
# fan-out saturated the worker threadpool, starved the asyncio event loop, and
# dropped the live websocket every time the Data page's Overview tab mounted.
# Computing the reports server-side in ONE sequential, TTL-cached pass keeps the
# heavy work off that fan-out so it can never again starve the loop.
_QUALITY_REPORTS_TTL_SECONDS = 120.0
# Keyed on (data root, limit) so distinct lakes (e.g. per-test tmp dirs) never
# share an entry.
_quality_reports_cache: dict[tuple[str, int], tuple[float, list[dict]]] = {}
_quality_reports_lock = threading.Lock()


def _quality_score(q: dict) -> float:
    """Mirror of the frontend's computeFallbackQualityScore so the leaderboard
    score is identical whether it comes from this route or the legacy fallback."""
    row_count = max(1, int(q.get("row_count") or 0))
    total_cells = max(1, row_count * 5)
    integrity = q.get("integrity") or {}
    outliers = q.get("outliers") or {}
    freshness = q.get("freshness") or {}
    score = 100.0
    score -= min(30.0, (int(q.get("gaps") or 0) / row_count) * 1000)
    score -= min(20.0, (int(q.get("null_values") or 0) / total_cells) * 1000)
    score -= min(10.0, int(integrity.get("invalid_high_low") or 0) * 2)
    score -= min(10.0, int(integrity.get("invalid_close_range") or 0) * 2)
    if freshness.get("is_stale"):
        score -= 10.0
    outlier_ratio = (int(outliers.get("close") or 0) + int(outliers.get("volume") or 0)) / row_count
    score -= min(10.0, outlier_ratio * 500)
    return max(0.0, min(100.0, round(score * 10) / 10))


def _quality_report_from(ds: dict, q: dict, idx: int) -> dict:
    integrity = q.get("integrity") or {}
    outliers = q.get("outliers") or {}
    freshness = q.get("freshness") or {}
    price = q.get("price_range") or {}
    vol = q.get("volume_stats") or {}
    symbol = _to_ui_symbol(q.get("symbol") or ds.get("symbol"))
    timeframe = str(q.get("timeframe") or ds.get("timeframe") or "")
    return {
        "id": f"quality-{idx}-{symbol}-{timeframe}",
        "symbol": symbol,
        "timeframe": timeframe,
        "row_count": int(q.get("row_count") or 0),
        "start_ts": q.get("start"),
        "end_ts": q.get("end"),
        "duration_days": float(q.get("duration_days") or 0.0),
        "gaps": int(q.get("gaps") or 0),
        "gap_details": q.get("gap_details") or [],
        "null_values": int(q.get("null_values") or 0),
        "price_range_min": float(price.get("min") or 0.0),
        "price_range_max": float(price.get("max") or 0.0),
        "volume_min": float(vol.get("min") or 0.0),
        "volume_max": float(vol.get("max") or 0.0),
        "volume_avg": float(vol.get("avg") or 0.0),
        "outliers_close": int(outliers.get("close") or 0),
        "outliers_volume": int(outliers.get("volume") or 0),
        "invalid_high_low": int(integrity.get("invalid_high_low") or 0),
        "invalid_close_range": int(integrity.get("invalid_close_range") or 0),
        "freshness_hours": float(freshness.get("hours_ago") or 0.0),
        "is_stale": bool(freshness.get("is_stale") or False),
        "quality_score": _quality_score(q),
        "computed_at": _now(),
    }


def _compute_quality_reports(limit: int) -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor

    from forven.data import compute_data_quality

    datasets = get_datasets_stub(remote_skip=True)
    datasets = sorted(
        (d for d in datasets if isinstance(d, dict)),
        key=lambda d: core._to_datetime_sort_key(d.get("end_ts") or d.get("start_ts")),
        reverse=True,
    )[:limit]

    def _one(ds: dict) -> tuple[dict, dict] | None:
        symbol = str(ds.get("symbol") or "").strip()
        timeframe = str(ds.get("timeframe") or "").strip()
        if not symbol or not timeframe:
            return None
        try:
            q = compute_data_quality(symbol, timeframe)
        except Exception:
            # A single unreadable/missing series must not sink the whole report.
            return None
        return (ds, q) if isinstance(q, dict) else None

    # Bounded parallelism: parquet reads release the GIL, so a few workers cut the
    # cold-cache build time without the 100-wide saturation that started all this.
    reports: list[dict] = []
    if not datasets:
        return reports
    with ThreadPoolExecutor(max_workers=min(4, len(datasets))) as pool:
        for pair in pool.map(_one, datasets):  # map preserves recency order
            if pair is None:
                continue
            ds, q = pair
            reports.append(_quality_report_from(ds, q, len(reports)))
    return reports


def get_quality_reports(limit: int = 100) -> list[dict]:
    """Server-side data-quality leaderboard, computed once and cached briefly.

    Remote data mode owns its own catalog, so we don't scan the local lake there
    — return an empty list (the UI shows "no reports yet") rather than proxying a
    slow per-series fan-out.
    """
    remote_enabled, _ = _remote_data_engine_config()
    if remote_enabled:
        return []

    from forven.data import DATA_DIR

    cache_limit = max(1, min(int(limit or 100), 500))
    cache_key = (str(DATA_DIR), cache_limit)
    now = time.time()
    cached = _quality_reports_cache.get(cache_key)
    if cached and (now - cached[0]) < _QUALITY_REPORTS_TTL_SECONDS:
        return cached[1]

    # Serialize recomputation so a burst of concurrent callers shares one pass
    # instead of each launching its own full-lake scan.
    with _quality_reports_lock:
        cached = _quality_reports_cache.get(cache_key)
        now = time.time()
        if cached and (now - cached[0]) < _QUALITY_REPORTS_TTL_SECONDS:
            return cached[1]
        reports = _compute_quality_reports(cache_limit)
        _quality_reports_cache[cache_key] = (time.time(), reports)
        return reports


def get_quality_report(symbol: str, timeframe: str) -> dict:
    """Single-series quality report in leaderboard row shape.

    The frontend has always called /data/quality/reports/{symbol}/{timeframe};
    the route never existed server-side, so every call 404'd into a client-side
    recompute fallback. Serve it from the cached leaderboard when present,
    else compute the one series directly.
    """
    from forven.data import compute_data_quality, symbol_to_fs

    fs_symbol = symbol_to_fs(symbol)
    ui_symbol = _to_ui_symbol(fs_symbol)
    for cached_at, reports in list(_quality_reports_cache.values()):
        if (time.time() - cached_at) >= _QUALITY_REPORTS_TTL_SECONDS:
            continue
        for report in reports:
            if report.get("symbol") == ui_symbol and report.get("timeframe") == timeframe:
                return report
    try:
        q = compute_data_quality(fs_symbol, timeframe)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _quality_report_from({"symbol": fs_symbol, "timeframe": timeframe}, q, 0)


def get_dataset_versions(symbol: str | None = None, timeframe: str | None = None, limit: int = 50) -> list[dict]:
    """Dataset version history — REAL, backed by the point-in-time revision log.

    The frontend has always called /data/versions; no server route existed, so
    it 404'd into a client-side reconstruction with checksum=None. Rows:
    - one "current" row per series (live snapshot; checksum included only for
      a single-series query — hashing every file on the unfiltered call is a
      full-lake read);
    - one row per RESTATEMENT event (bars superseded at the same observed_at),
      from the append-only revision lake.
    """
    from forven.data import compute_checksum, scan_datasets, symbol_to_fs
    from forven.dataeng.revisions import read_revisions, revisions_root

    fs_filter = symbol_to_fs(symbol) if symbol else None
    single_series = bool(fs_filter and timeframe)
    rows: list[dict] = []

    for ds in scan_datasets():
        ds_symbol = str(ds.get("symbol") or "")
        ds_tf = str(ds.get("timeframe") or "")
        if fs_filter and ds_symbol != fs_filter:
            continue
        if timeframe and ds_tf != timeframe:
            continue
        rows.append(
            {
                "id": f"current-{ds_symbol}-{ds_tf}",
                "symbol": _to_ui_symbol(ds_symbol),
                "timeframe": ds_tf,
                "source": ds.get("source") or "local",
                "row_count": int(ds.get("row_count") or 0),
                "start_ts": ds.get("start_ts"),
                "end_ts": ds.get("end_ts"),
                "checksum": compute_checksum(ds_symbol, ds_tf) if single_series else None,
                "ingestion_run_id": None,
                "created_at": ds.get("end_ts") or ds.get("start_ts") or _now(),
            }
        )

    # Restatement events: enumerate only series that HAVE a revision log.
    root = revisions_root()
    if root.exists():
        for sym_dir in sorted(root.iterdir()):
            if not sym_dir.is_dir():
                continue
            if fs_filter and sym_dir.name != fs_filter:
                continue
            for rev_file in sorted(sym_dir.glob("*.parquet")):
                rev_tf = rev_file.stem
                if timeframe and rev_tf != timeframe:
                    continue
                try:
                    revisions = read_revisions(sym_dir.name, rev_tf)
                except Exception:
                    continue
                if revisions is None or revisions.empty:
                    continue
                grouped = revisions.groupby("observed_at")
                for observed_at, group in grouped:
                    ts = pd.to_datetime(group["timestamp"], utc=True, errors="coerce").dropna()
                    rows.append(
                        {
                            "id": f"rev-{sym_dir.name}-{rev_tf}-{observed_at}",
                            "symbol": _to_ui_symbol(sym_dir.name),
                            "timeframe": rev_tf,
                            "source": "restatement",
                            "row_count": int(len(group)),
                            "start_ts": ts.min().isoformat() if len(ts) else None,
                            "end_ts": ts.max().isoformat() if len(ts) else None,
                            "checksum": None,
                            "ingestion_run_id": None,
                            "created_at": str(observed_at),
                        }
                    )

    rows.sort(key=lambda row: core._to_datetime_sort_key(row.get("created_at")), reverse=True)
    return rows[: max(1, int(limit or 50))]


def get_quality_gate(symbol: str, timeframe: str, window_days: int | None = None) -> dict:
    """The exact fitness verdict the gauntlet's data gate applies before
    scoring a strategy on this series (completeness, interior gaps, freshness,
    listing window). Surfaces WHY a series is blocked in the UI instead of the
    operator discovering it via a stalled gauntlet step."""
    import pandas as pd

    from forven.dataeng.quality_gate import check_series_quality

    window_start = None
    if window_days and int(window_days) > 0:
        window_start = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=int(window_days))
    return check_series_quality(symbol, timeframe, window_start=window_start).as_dict()


def get_data_health():
    """Return merged DB/parquet health + per-stream freshness snapshot.

    The legacy body (db_path, db_size_bytes, dataset_count, etc.) from
    :func:`forven.data.compute_data_health` is preserved so the frontend
    (``frontend/src/lib/api/data.ts::getDataHealth``) keeps working.

    T23 extends this with a ``streams`` dict keyed by stream name
    (ohlcv/funding/oi/...) sourced from :func:`data_manager_stats`, plus
    a top-level ``generated_at`` ISO timestamp. Streams that have never
    been collected since process start carry ``{"status": "never_ran"}``.
    """
    from forven.data import compute_data_health
    from forven.data_manager import _now_iso, data_manager_stats

    try:
        legacy = compute_data_health()
    except Exception as exc:
        log.error("Failed to compute data health: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    try:
        stats = data_manager_stats()
    except Exception as exc:
        log.error("Failed to read data manager stats: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    known = [
        "ohlcv",
        "funding",
        "oi",
        "long_short_ratio",
        "taker_volume",
        "liquidations",
        "fear_greed",
        "macro",
        "btc_dominance",
    ]
    streams: dict[str, object] = {
        s: stats.get(s, {"status": "never_ran"}) for s in known
    }

    if not isinstance(legacy, dict):
        try:
            legacy = legacy.dict()  # pydantic v1
        except AttributeError:
            legacy = dict(legacy)

    return {**legacy, "streams": streams, "generated_at": _now_iso()}


def get_collection_health() -> dict:
    """Plain-language per-stream collection health + aggregate score for the
    /data source-health panel. Maps persisted telemetry's consecutive_failures to
    Healthy / Recovering / Down and sorts worst-first."""
    from forven.data_manager import data_manager_stats
    from forven.health_monitor import DATA_STREAM_FAILURE_RED, data_health_score

    try:
        stats = data_manager_stats()
    except Exception as exc:
        log.error("Failed to read collection telemetry: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    known = [
        "ohlcv", "funding", "oi", "long_short_ratio", "taker_volume",
        "liquidations", "fear_greed", "macro", "btc_dominance",
    ]
    order = {"down": 0, "recovering": 1, "healthy": 2, "never_ran": 3}
    streams: list[dict] = []
    for name in known:
        entry = stats.get(name)
        if not isinstance(entry, dict) or not entry.get("total_calls"):
            streams.append({
                "stream": name, "status": "never_ran", "consecutive_failures": 0,
                "last_success": None, "last_run": None, "last_error": None, "total_rows": 0,
            })
            continue
        cf = int(entry.get("consecutive_failures", 0) or 0)
        status = "down" if cf >= DATA_STREAM_FAILURE_RED else ("recovering" if cf > 0 else "healthy")
        streams.append({
            "stream": name,
            "status": status,
            "consecutive_failures": cf,
            "last_success": entry.get("last_success_ts"),
            "last_run": entry.get("last_run_ts"),
            "last_error": entry.get("last_error"),
            "total_rows": int(entry.get("total_rows", 0) or 0),
        })
    streams.sort(key=lambda s: order.get(s["status"], 9))
    return {"score": data_health_score(), "streams": streams}


def get_data_activity(limit: int = 200) -> dict:
    """Unified chronological log of data actions for the /data Activity tab.

    Merges the audit trail of maintenance actions (backfills, source
    reconciliation — ``activity_log`` rows with ``source='data'``) with genuine
    download runs (``get_active_ingestion_runs``). Reconstructed catalog rows are
    deliberately excluded: this is an *actions* log, not a dataset snapshot.
    """
    import json as _json

    from forven.db import get_db

    events: list[dict] = []

    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT created_at, level, message, data FROM activity_log "
                "WHERE source = 'data' ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        for row in rows:
            try:
                detail = _json.loads(row["data"]) if row["data"] else {}
            except Exception:
                detail = {}
            if not isinstance(detail, dict):
                detail = {}
            events.append({
                "ts": row["created_at"],
                "level": str(row["level"] or "info"),
                "action": str(detail.get("action") or "event"),
                "message": str(row["message"] or ""),
                "detail": detail,
            })
    except Exception as exc:
        log.debug("activity_log read failed: %s", exc)

    try:
        from forven.data import get_active_ingestion_runs

        for run in get_active_ingestion_runs() or []:
            status = str(run.get("status") or "")
            symbol = run.get("symbol")
            timeframe = run.get("timeframe")
            source = run.get("source") or "?"
            bars = int(run.get("bars_new") or 0) or int(run.get("bars_fetched") or 0)
            if status == "failed":
                message = f"Download failed: {symbol} {timeframe} from {source}"
            else:
                message = f"Downloaded {symbol} {timeframe} from {source} — {bars:,} bars"
            events.append({
                "ts": run.get("completed_at") or run.get("started_at"),
                "level": "error" if status == "failed" else "info",
                "action": "download",
                "message": message,
                "detail": {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "source": source,
                    "status": status,
                    "bars": bars,
                    "error": run.get("error"),
                },
            })
    except Exception as exc:
        log.debug("ingestion-run read failed: %s", exc)

    events.sort(key=lambda event: str(event.get("ts") or ""), reverse=True)
    return {"events": events[: int(limit)], "generated_at": _now()}


def post_scan_orphans() -> dict:
    """Read-only storage-drift scan (leftover temp files + unreadable/empty parquet).
    Drives the Maintenance tab's orphan panel; logs to the Data Log only on drift."""
    from forven.data import scan_parquet_orphans

    try:
        return scan_parquet_orphans()
    except Exception as exc:
        log.error("Orphan scan failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def post_cleanup_orphans() -> dict:
    """Delete the storage-drift artifacts the scan found. Logs an orphan_cleanup
    action to the Data Log when anything is removed."""
    from forven.data import cleanup_parquet_orphans

    try:
        return cleanup_parquet_orphans()
    except Exception as exc:
        log.error("Orphan cleanup failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def get_data_engine_status() -> dict:
    from forven.dataeng.hub import get_data_hub

    try:
        return get_data_hub().status()
    except Exception as exc:
        log.error("Failed to read DataHub status: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def post_data_engine_backfill_plan() -> dict:
    from forven.dataeng.catalog import Catalog
    from forven.dataeng.catchup import CatchUpPlanner

    # Refresh coverage from the lake first, so the plan reflects bars written since
    # the last scan (collect/backfill update the parquet lake but NOT the catalog —
    # without this the plan never drains after an Execute).
    catalog = Catalog()
    try:
        catalog.scan_lake()
    except Exception as exc:
        log.warning("Backfill plan: lake scan failed, using existing coverage: %s", exc)
    try:
        tasks = CatchUpPlanner(catalog=catalog).plan()
    except Exception as exc:
        log.error("Failed to plan Data Engine backfill: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "task_count": len(tasks),
        "tasks": [
            {
                "source": task.source,
                "market": task.market,
                "symbol": task.symbol,
                "timeframe": task.timeframe,
                "stream": task.stream,
                "start_ts": task.start_ts,
                "end_ts": task.end_ts,
                "permanent": task.permanent,
            }
            for task in tasks
        ],
    }


# Series whose last catch-up attempt stalled (added 0 bars and couldn't fetch
# newer data — delisted symbol, unfillable gap, persistent fetch error). They
# are deprioritized for a cooldown window so a permanently-stalled
# alphabetically-first series can't monopolize every 10-minute batch slot.
# Process-local is fine: the job runs in-process and re-plans from the lake.
_CATCHUP_STALL_COOLDOWN_SECS = 6 * 3600.0
_catchup_stalled: dict[tuple[str, str], float] = {}


def execute_data_engine_catchup(
    max_tasks: int = 10, *, cap: int = 50, deadline_seconds: float | None = None
) -> dict:
    """Run a bounded batch of the Data Engine candle catch-up plan and return a
    summary.

    Pure (raises plain exceptions, never ``HTTPException``) so both the HTTP
    endpoint and the scheduled ``forven-data-engine-catchup`` auto-drain job can
    call it. Uses ``backfill_ohlcv_gaps`` (reports bars_added + a no_recent_data
    flag) so a series that genuinely can't advance is counted as ``failed`` rather
    than silently reported as a green success.

    ``deadline_seconds`` is a wall-clock budget: the batch stops gracefully once
    it is exceeded (returning partial progress; the next run continues the drain).
    The scheduler passes a value below its own job timeout so this job always
    returns in time instead of overrunning — an overrun can't be killed (Python
    threads), leaving a zombie thread that holds the scheduler lock.
    """
    job_start = time.monotonic()

    # Self-healing coverage: ensure the generation universe (scan symbols × screen and
    # sweep timeframes) has enough history for the screen window, triggering async
    # backfills for any shortfall. The planner below only keeps EXISTING catalog series
    # current — it never adds history a generated strategy needs. Cheap when already
    # covered; non-blocking (submit_ingestion is async). Never fail the drain on it.
    try:
        from forven.dataeng.coverage import ensure_universe_coverage

        ensure_universe_coverage()
    except Exception as exc:  # noqa: BLE001
        log.warning("Data Engine catch-up: universe coverage ensure skipped: %s", exc)

    # Keep the symbol registry current (new listings, delistings) on the same
    # cadence — one markets+tickers call per 30-min run. Best-effort, and
    # gated on the same network switch as auto-backfill so the test suite
    # never hits the venue (load_markets + fetch_tickers hang/slow tests).
    try:
        from forven.dataeng.coverage import _autobackfill_enabled
        from forven.dataeng.universe import refresh_symbol_registry

        if _autobackfill_enabled():
            refresh_symbol_registry()
    except Exception as exc:  # noqa: BLE001
        log.warning("Data Engine catch-up: symbol registry refresh skipped: %s", exc)

    from forven.dataeng.catalog import Catalog
    from forven.dataeng.catchup import CatchUpPlanner

    # Refresh coverage from the parquet lake BEFORE planning. backfill writes
    # bars to parquet but nothing else updates the DuckDB series_coverage
    # table (scan_lake is its sole writer), so without this rescan the
    # scheduled job re-plans — and re-executes — the same alphabetically-first
    # batch forever and the backlog never drains autonomously.
    catalog = Catalog()
    try:
        catalog.scan_lake()
    except Exception as exc:
        log.warning("Data Engine catch-up: lake scan failed, using existing coverage: %s", exc)
    tasks = CatchUpPlanner(catalog=catalog).plan()

    # The planner emits OHLCV (candles) catch-up tasks; trades/orderbook are
    # microstructure streams not collected through this path.
    candle_tasks = [t for t in tasks if str(t.stream or "").lower() == "candles"]

    # Stable sort: series that stalled recently go to the back of the queue so
    # the bounded batch advances past them instead of retrying the same
    # unfillable head every run.
    now_mono = time.monotonic()
    candle_tasks.sort(
        key=lambda t: (
            1
            if (now_mono - _catchup_stalled.get((t.symbol, t.timeframe), -_CATCHUP_STALL_COOLDOWN_SECS))
            < _CATCHUP_STALL_COOLDOWN_SECS
            else 0
        )
    )
    batch = candle_tasks[: max(1, min(int(max_tasks or 10), cap))]

    from forven.data import backfill_ohlcv_gaps

    executed = rows_added = failed = 0
    deadline_hit = False
    results: list[dict] = []
    for t in batch:
        # Wall-clock budget: stop before the scheduler's job timeout so this job
        # always returns (an overrun leaves an unkillable zombie thread holding
        # the scheduler lock). Partial progress is fine — the next run continues.
        if deadline_seconds is not None and (time.monotonic() - job_start) >= deadline_seconds:
            deadline_hit = True
            log.warning(
                "Data Engine catch-up: %.0fs deadline reached after %d/%d task(s) — "
                "stopping; next run continues the drain.",
                deadline_seconds, executed, len(batch),
            )
            break
        executed += 1
        try:
            res = backfill_ohlcv_gaps(t.symbol, t.timeframe)
            added = int(res.get("bars_added") or 0)
            rows_added += added
            # A task that added no bars AND couldn't fetch newer data genuinely
            # stalled (delisted / fetch failure) — not a green "success".
            stalled = added == 0 and bool(res.get("no_recent_data"))
            if stalled:
                failed += 1
                _catchup_stalled[(t.symbol, t.timeframe)] = time.monotonic()
            else:
                _catchup_stalled.pop((t.symbol, t.timeframe), None)
            results.append(
                {"symbol": t.symbol, "timeframe": t.timeframe, "rows_added": added, "stalled": stalled}
            )
        except Exception as exc:
            failed += 1
            _catchup_stalled[(t.symbol, t.timeframe)] = time.monotonic()
            results.append({"symbol": t.symbol, "timeframe": t.timeframe, "error": str(exc)[:200]})

    try:
        from forven.data import _log_data_action

        _log_data_action(
            "backfill",
            f"Executed Data Engine backfill plan: {executed} task(s), +{rows_added:,} bars, "
            f"{failed} failed",
            level="warning" if failed else "info",
            executed=executed,
            failed=failed,
            rows_added=rows_added,
        )
    except Exception:
        pass

    return {
        "planned_total": len(tasks),
        "candle_total": len(candle_tasks),
        "executed": executed,
        "rows_added": rows_added,
        "failed": failed,
        "deadline_hit": deadline_hit,
        "results": results[:50],
    }


def post_execute_data_engine_backfill(max_tasks: int = 10) -> dict:
    """Execute a bounded batch of the Data Engine catch-up plan, so the plan is
    actionable rather than preview-only.

    Thin HTTP wrapper around :func:`execute_data_engine_catchup`. Bounded per call;
    the caller re-plans afterward (the plan endpoint rescans the lake) to see the
    backlog drain. The same logic runs automatically via the scheduled
    ``forven-data-engine-catchup`` job.
    """
    try:
        return execute_data_engine_catchup(max_tasks)
    except Exception as exc:
        log.error("Failed to execute Data Engine backfill: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def post_backfill_gaps(symbol: str, timeframe: str, max_gaps: int | None = None) -> dict:
    """Execute a real gap backfill for a stored OHLCV series (vs the plan-only
    engine endpoint). Detects internal gaps and fetches each missing range."""
    from forven.data import backfill_ohlcv_gaps

    sym = str(symbol or "").strip()
    tf = str(timeframe or "").strip()
    if not sym or not tf:
        raise HTTPException(status_code=400, detail="symbol and timeframe are required")
    try:
        return backfill_ohlcv_gaps(sym, tf, max_gaps=max_gaps)
    except Exception as exc:
        log.error("Gap backfill failed for %s %s: %s", sym, tf, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def post_upload_csv_preview(content: bytes):
    from forven.data import preview_csv

    try:
        return preview_csv(content)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def post_upload_csv(
    content: bytes,
    filename: str,
    symbol: str,
    timeframe: str,
    timestamp_column: str | None = None,
    date_format: str | None = None,
):
    from forven.data import process_csv_upload

    try:
        payload = process_csv_upload(
            content=content,
            filename=filename,
            symbol=symbol,
            timeframe=timeframe,
            ts_col=timestamp_column,
            date_format=date_format,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.error("CSV upload failed for %s %s: %s", symbol, timeframe, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if isinstance(payload, dict):
        payload = dict(payload)
        payload["symbol"] = _to_ui_symbol(payload.get("symbol"))
    return payload


def get_dataset_export(symbol: str, timeframe: str, format: str = "csv") -> tuple[bytes, str, str]:
    from forven.data import export_dataset_bytes

    try:
        return export_dataset_bytes(symbol=symbol, timeframe=timeframe, format=format)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.error("Dataset export failed for %s %s: %s", symbol, timeframe, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def get_symbol_export(symbol: str, format: str = "csv") -> tuple[bytes, str]:
    from forven.data import export_symbol_zip

    try:
        return export_symbol_zip(symbol=symbol, format=format)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.error("Symbol export failed for %s: %s", symbol, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _build_ohlcv_response(symbol: str, timeframe: str = "1h", limit: int = 100) -> dict:
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    requested_limit = max(min(int(limit or 100), 2000), 1)
    requested_tf = str(timeframe or "1h").strip().lower() or "1h"
    # These live in the trading domain, not api_core — referencing them as
    # core._* raised AttributeError, 500-ing /api/ohlcv (the OHLCV fallback path).
    from forven.api_domains.trading import _coerce_iso_timestamp, _normalize_asset_key

    asset = _normalize_asset_key(normalized_symbol)
    if not asset:
        raise HTTPException(status_code=400, detail=f"invalid symbol: {symbol}")

    resolved_tf = requested_tf
    frame = None
    try:
        frame = fetch_hyperliquid_candles(asset, bars=requested_limit, interval=resolved_tf)
    except Exception:
        if requested_tf != "1h":
            try:
                resolved_tf = "1h"
                frame = fetch_hyperliquid_candles(asset, bars=requested_limit, interval=resolved_tf)
            except Exception:
                frame = None

    if frame is None or frame.empty:
        now_iso = _now()
        return {
            "symbol": normalized_symbol,
            "timeframe": resolved_tf,
            "source": "hyperliquid",
            "start": now_iso,
            "end": now_iso,
            "row_count": 0,
            "data": [],
        }

    rows = []
    for timestamp, row in frame.tail(requested_limit).iterrows():
        iso = _coerce_iso_timestamp(getattr(timestamp, "isoformat", lambda: str(timestamp))())
        if not iso:
            continue
        rows.append(
            {
                "timestamp": iso,
                "open": float(row.get("open", 0.0)),
                "high": float(row.get("high", 0.0)),
                "low": float(row.get("low", 0.0)),
                "close": float(row.get("close", 0.0)),
                "volume": float(row.get("volume", 0.0)),
            }
        )

    if not rows:
        now_iso = _now()
        return {
            "symbol": normalized_symbol,
            "timeframe": resolved_tf,
            "source": "hyperliquid",
            "start": now_iso,
            "end": now_iso,
            "row_count": 0,
            "data": [],
        }

    return {
        "symbol": normalized_symbol,
        "timeframe": resolved_tf,
        "source": "hyperliquid",
        "is_fallback": resolved_tf != requested_tf,
        "start": rows[0]["timestamp"],
        "end": rows[-1]["timestamp"],
        "row_count": len(rows),
        "data": rows,
    }


def get_dataset_ohlcv(symbol: str, timeframe: str, limit: int = 100, remote_skip: bool = False):
    remote_enabled, remote_url = _remote_data_engine_config()
    if remote_enabled and not remote_skip:
        encoded_symbol = urllib.parse.quote(str(symbol or "").strip(), safe="")
        encoded_tf = urllib.parse.quote(str(timeframe or "").strip(), safe="")
        payload, _ = _request_remote_json(
            "GET",
            _remote_endpoint_candidates(remote_url, f"/datasets/{encoded_symbol}/{encoded_tf}/ohlcv"),
            params={"limit": max(1, int(limit))},
            timeout=15.0,
        )
        if isinstance(payload, dict):
            current = dict(payload)
            current["symbol"] = _to_ui_symbol(current.get("symbol"))
            return current
        raise HTTPException(
            status_code=502,
            detail="Remote dataset OHLCV endpoint returned invalid payload.",
        )

    from forven.data import dataset_ohlcv

    try:
        payload = dataset_ohlcv(symbol=symbol, timeframe=timeframe, limit=limit)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        log.error("Failed to read dataset OHLCV for %s %s: %s", symbol, timeframe, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if isinstance(payload, dict):
        payload = dict(payload)
        payload["symbol"] = _to_ui_symbol(payload.get("symbol"))
    return payload


def get_ohlcv(symbol: str, timeframe: str = "1h", limit: int = 100):
    return _build_ohlcv_response(symbol=symbol, timeframe=timeframe, limit=limit)


def get_symbols_stub():
    symbols = {
        str(row.get("symbol") or "").strip()
        for row in get_datasets_stub()
        if str(row.get("symbol") or "").strip()
    }
    return sorted(symbols)


def get_sources_stub():
    try:
        from forven.data import list_data_sources

        return list_data_sources()
    except Exception:
        return []


def get_data_sources_stub():
    return get_sources_stub()


def get_source_symbols_stub(source: str, query: str | None = None, exchange: str | None = None):
    try:
        from forven.data import search_source_symbols

        rows = search_source_symbols(source, query=query, limit=200, exchange=exchange)
    except Exception:
        return []

    normalized: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        current = dict(row)
        current["symbol"] = _to_ui_symbol(current.get("symbol"))
        normalized.append(current)
    return normalized


def search_source_symbols_stub(
    source: str | None = None, query: str | None = None, exchange: str | None = None
):
    normalized_source = str(source or "").strip().lower()
    if normalized_source:
        return get_source_symbols_stub(normalized_source, query=query, exchange=exchange)

    merged: list[dict[str, object]] = []
    seen: set[str] = set()
    for candidate in ("ccxt", "binance"):
        for row in get_source_symbols_stub(candidate, query=query, exchange=exchange):
            symbol = str(row.get("symbol") or "").strip()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            merged.append(row)
            if len(merged) >= 200:
                return merged
    return merged


# ---------------------------------------------------------------------------
# DataManager stream health & collection endpoints
# ---------------------------------------------------------------------------

_collect_debounce: dict[tuple[str, str], float] = {}
_collect_debounce_lock = threading.Lock()
_COLLECT_DEBOUNCE_SECS = 60.0

# Cadences per stream (seconds) — amber if age > 2×
_STREAM_CADENCES = {
    "ohlcv": 900,       # 15 min
    "funding": 28800,   # 8 h
    "oi": 3600,         # 1 h
}




def _stream_health_from_footer(row_count: int, last_ms: int | None, cadence_secs: int) -> dict:
    """Stream health from footer stats only — NEVER a full column/series load.
    The previous implementation loaded the ENTIRE series into pandas just to
    read the last timestamp; on the post-backfill 1m series (millions of rows)
    that made the Datasets tab take seconds per click on the single worker."""
    if not row_count or last_ms is None:
        return {"status": "no_data", "row_count": 0, "last_updated": None, "data_age_hours": None}
    now = datetime.now(timezone.utc)
    last_ts = pd.Timestamp(last_ms, unit="ms", tz="UTC")
    age_secs = (now - last_ts).total_seconds()
    return {
        "status": "live" if age_secs <= cadence_secs * 2 else "accumulating",
        "row_count": int(row_count),
        "last_updated": last_ts.isoformat(),
        "data_age_hours": round(age_secs / 3600, 2),
    }


def _footer_stream_stats(path) -> tuple[int, int | None]:
    """(row_count, last_ms) for a stream parquet from its footer; (0, None)
    when absent/unreadable."""
    from forven.data import _footer_bounds

    try:
        if not path.exists():
            return 0, None
        rows, _, last_ms = _footer_bounds(path)
        return rows, last_ms
    except Exception:
        return 0, None


def get_stream_health(symbol: str) -> dict:
    """Return health for OHLCV, Funding, and OI streams for a symbol."""
    try:
        from forven.data import _series_row_count, dataset_last_timestamp_ms, symbol_to_fs
        from forven.data_manager import FUNDING_DIR, OI_DIR, data_manager
        fs_symbol = symbol_to_fs(symbol)

        # OHLCV — use most recently active timeframe (footer reads only)
        timeframes = data_manager.get_active_timeframes(symbol)
        tf = next(iter(timeframes)) if timeframes else "1h"
        ohlcv_health = _stream_health_from_footer(
            _series_row_count(symbol, tf),
            dataset_last_timestamp_ms(symbol, tf),
            _STREAM_CADENCES["ohlcv"],
        )
        ohlcv_health["timeframe"] = tf

        # Funding
        funding_rows, funding_last = _footer_stream_stats(FUNDING_DIR / fs_symbol / "history.parquet")
        funding_health = _stream_health_from_footer(funding_rows, funding_last, _STREAM_CADENCES["funding"])

        # OI — first timeframe with data
        oi_rows, oi_last = 0, None
        for t in list(timeframes) + ["1h", "4h"]:
            oi_rows, oi_last = _footer_stream_stats(OI_DIR / fs_symbol / f"{t}.parquet")
            if oi_rows:
                break
        oi_health = _stream_health_from_footer(oi_rows, oi_last, _STREAM_CADENCES["oi"])

        # Source reason — two scalar per-symbol counts. The previous code first
        # computed the WHOLE active set (an ~1s unindexed backtest_results scan)
        # just to decide whether to run these counts; the counts themselves
        # already answer the question.
        reason = None
        try:
            from forven.db import get_db
            from datetime import timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            with get_db() as conn:
                strat_count = conn.execute(
                    "SELECT COUNT(*) FROM strategies WHERE symbol = ? AND stage IN ('paper', 'paper_trading', 'live_graduated', 'deployed', 'gauntlet', 'active')",
                    (fs_symbol,),
                ).fetchone()[0]
                bt_count = conn.execute(
                    "SELECT COUNT(DISTINCT id) FROM backtest_results WHERE symbol = ? AND created_at >= ? AND deleted_at IS NULL",
                    (fs_symbol, cutoff),
                ).fetchone()[0]
            parts = []
            if strat_count:
                parts.append(f"{strat_count} active {'strategy' if strat_count == 1 else 'strategies'}")
            if bt_count:
                parts.append(f"{bt_count} recent {'backtest' if bt_count == 1 else 'backtests'}")
            reason = ", ".join(parts) if parts else None
        except Exception:
            reason = None

        return {
            "symbol": symbol,
            "streams": {
                "ohlcv": ohlcv_health,
                "funding": funding_health,
                "oi": oi_health,
            },
            "collection_reason": reason,
        }
    except Exception as exc:
        log.warning("get_stream_health failed for %s: %s", symbol, exc)
        raise HTTPException(status_code=500, detail=str(exc))


def get_stream_rows(
    symbol: str, stream: str, timeframe: str | None = None, limit: int = 500
) -> dict:
    """Return recent raw rows for a non-OHLCV stream (funding/oi) for the data viewer.

    OHLCV already has its own series endpoint; this surfaces the enrichment streams
    so the operator can actually see the stored funding-rate / open-interest values.
    Returns ``{symbol, stream, timeframe, columns, rows}`` (generic table shape).
    """
    stream = str(stream or "").strip().lower()
    if stream not in ("funding", "oi"):
        raise HTTPException(status_code=400, detail=f"Unsupported stream for rows: {stream}")
    empty = {"symbol": symbol, "stream": stream, "timeframe": None, "columns": [], "rows": []}
    try:
        from forven.data import symbol_to_fs
        from forven.data_manager import FUNDING_DIR, OI_DIR, _load_stream_parquet, data_manager

        fs_symbol = symbol_to_fs(symbol)
        df = None
        resolved_tf = None
        if stream == "funding":
            df = _load_stream_parquet(FUNDING_DIR / fs_symbol / "history.parquet")
        else:  # oi is stored per-timeframe; try the requested tf, then the active set
            candidates: list[str] = []
            if timeframe:
                candidates.append(str(timeframe))
            # sorted() so the resolved tf is deterministic (active TFs are a set)
            for t in sorted(data_manager.get_active_timeframes(symbol)) + ["1h", "4h"]:
                if t not in candidates:
                    candidates.append(t)
            for t in candidates:
                cand = _load_stream_parquet(OI_DIR / fs_symbol / f"{t}.parquet")
                if cand is not None and len(cand):
                    df, resolved_tf = cand, t
                    break

        if df is None or len(df) == 0:
            return {**empty, "timeframe": resolved_tf}

        out = df.sort_values("timestamp").tail(max(1, int(limit))).copy()
        if "timestamp" in out.columns:
            out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True).dt.strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        from forven.util import sanitize_json_floats

        return {
            "symbol": symbol,
            "stream": stream,
            "timeframe": resolved_tf,
            "columns": [str(c) for c in out.columns],
            # A stored NaN/inf (provider gap) would otherwise 500 the JSONResponse.
            "rows": sanitize_json_floats(out.to_dict(orient="records")),
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("get_stream_rows failed for %s/%s: %s", symbol, stream, exc)
        return empty


def post_collect_stream(symbol: str, stream: str) -> dict:
    """Trigger immediate collection for a single stream. Enforces 60s debounce."""
    stream = stream.lower()
    if stream not in ("ohlcv", "funding", "oi"):
        raise HTTPException(status_code=400, detail=f"Unknown stream: {stream}")

    key = (symbol, stream)
    with _collect_debounce_lock:
        last = _collect_debounce.get(key)
        if last is not None and (time.monotonic() - last) < _COLLECT_DEBOUNCE_SECS:
            remaining = int(_COLLECT_DEBOUNCE_SECS - (time.monotonic() - last))
            raise HTTPException(status_code=429, detail=f"Debounced — try again in {remaining}s")
        _collect_debounce[key] = time.monotonic()

    try:
        from forven.data_manager import data_manager
        if stream == "ohlcv":
            timeframes = data_manager.get_active_timeframes(symbol)
            rows_added = 0
            for tf in timeframes:
                rows_added += int(data_manager._ohlcv.collect(symbol, tf) or 0)
        elif stream == "funding":
            rows_added = data_manager._funding.collect(symbol)
        else:
            timeframes = data_manager.get_active_timeframes(symbol)
            rows_added = 0
            for tf in timeframes:
                rows_added += data_manager._oi.collect(symbol, tf)
        return {"status": "ok", "symbol": symbol, "stream": stream, "rows_added": rows_added}
    except Exception as exc:
        log.warning("post_collect_stream failed for %s/%s: %s", symbol, stream, exc)
        raise HTTPException(status_code=500, detail=str(exc))


def get_active_symbols_with_reasons() -> list[dict]:
    """Return active symbols with strategy/backtest counts as reasons.

    Two GROUP BY queries on one connection instead of the previous
    two-queries-per-symbol N+1 (matching semantics unchanged: exact
    symbol-string equality)."""
    try:
        from forven.data_manager import data_manager
        from forven.db import get_db
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        symbols = data_manager.get_active_symbols()
        strat_counts: dict[str, int] = {}
        bt_counts: dict[str, int] = {}
        try:
            with get_db() as conn:
                for sym, count in conn.execute(
                    "SELECT symbol, COUNT(*) FROM strategies WHERE stage IN ('paper', 'paper_trading', 'live_graduated', 'deployed', 'gauntlet', 'active') GROUP BY symbol"
                ).fetchall():
                    strat_counts[str(sym)] = int(count)
                for sym, count in conn.execute(
                    "SELECT symbol, COUNT(DISTINCT id) FROM backtest_results WHERE created_at >= ? AND deleted_at IS NULL GROUP BY symbol",
                    (cutoff,),
                ).fetchall():
                    bt_counts[str(sym)] = int(count)
        except Exception:
            pass
        return [
            {
                "symbol": symbol,
                "active_strategies": strat_counts.get(symbol, 0),
                "recent_backtests": bt_counts.get(symbol, 0),
            }
            for symbol in sorted(symbols)
        ]
    except Exception as exc:
        log.warning("get_active_symbols_with_reasons failed: %s", exc)
        return []


_backfill_lock = threading.Lock()
_backfill_cancel = threading.Event()
_BACKFILL_STATE_KV_KEY = "data:backfill_state"
_backfill_state: dict = {
    "running": False,
    "last_started_at": None,
    "last_result": None,
    "last_error": None,
    "progress": None,
    "cancel_requested": False,
}
_backfill_state_loaded = False


def _load_backfill_state_locked() -> None:
    """Seed last_result/last_error from KV once per process so the status
    endpoint survives a restart (it was a process-local dict that reset to
    empty). ``running`` is never restored — a restart kills the thread."""
    global _backfill_state_loaded
    if _backfill_state_loaded:
        return
    _backfill_state_loaded = True
    try:
        from forven.db import kv_get

        saved = kv_get(_BACKFILL_STATE_KV_KEY, None)
        if isinstance(saved, dict):
            for key in ("last_started_at", "last_result", "last_error"):
                if _backfill_state.get(key) is None:
                    _backfill_state[key] = saved.get(key)
    except Exception:
        pass


def _persist_backfill_state_locked() -> None:
    try:
        from forven.db import kv_set_best_effort

        kv_set_best_effort(
            _BACKFILL_STATE_KV_KEY,
            {
                "last_started_at": _backfill_state.get("last_started_at"),
                "last_result": _backfill_state.get("last_result"),
                "last_error": _backfill_state.get("last_error"),
            },
        )
    except Exception:
        pass


def get_backfill_status() -> dict:
    """Return current backfill state (running flag, per-symbol progress,
    last result/error — the latter restart-surviving via KV)."""
    with _backfill_lock:
        _load_backfill_state_locked()
        return dict(_backfill_state)


def post_cancel_backfill() -> dict:
    """Request a cooperative stop of the running BV backfill (takes effect
    between symbols)."""
    with _backfill_lock:
        if not _backfill_state["running"]:
            raise HTTPException(status_code=409, detail="No backfill running")
        _backfill_cancel.set()
        _backfill_state["cancel_requested"] = True
    return {"status": "cancelling"}


def post_trigger_backfill(symbol: str | None = None) -> dict:
    """Trigger a Binance Vision backfill in a background thread."""
    with _backfill_lock:
        if _backfill_state["running"]:
            raise HTTPException(status_code=409, detail="Backfill already running")
        _load_backfill_state_locked()
        _backfill_cancel.clear()
        _backfill_state.update(
            {
                "running": True,
                "last_started_at": _now(),
                "last_result": None,
                "last_error": None,
                "progress": None,
                "cancel_requested": False,
            }
        )

    def _on_progress(done: int, total: int, current_symbol: str) -> None:
        with _backfill_lock:
            _backfill_state["progress"] = {
                "done": int(done),
                "total": int(total),
                "current_symbol": current_symbol,
            }

    def _run() -> None:
        try:
            from forven.data_manager import data_manager
            result = data_manager.backfill(
                symbol=symbol, progress_cb=_on_progress, cancel_event=_backfill_cancel
            )
            with _backfill_lock:
                _backfill_state["running"] = False
                _backfill_state["last_result"] = result
                _backfill_state["progress"] = None
                _persist_backfill_state_locked()
        except Exception as exc:
            log.warning("post_trigger_backfill failed: %s", exc)
            with _backfill_lock:
                _backfill_state["running"] = False
                _backfill_state["last_error"] = str(exc)
                _backfill_state["progress"] = None
                _persist_backfill_state_locked()

    threading.Thread(target=_run, daemon=True, name="bv-backfill-ui").start()
    return {"status": "started", "symbol": symbol}


_DEPTH_CALIBRATION_KV_PREFIX = "data:depth_calibration:"


def get_depth_calibration(symbol: str) -> dict:
    """Stored empirical depth profile for a symbol (median/p25 resting notional
    per ±% level from BV bookDepth archives), or 404 when never computed."""
    from forven.data import symbol_to_fs
    from forven.db import kv_get

    fs_symbol = symbol_to_fs(symbol)
    payload = kv_get(f"{_DEPTH_CALIBRATION_KV_PREFIX}{fs_symbol}", None)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=404, detail=f"no depth calibration for {fs_symbol}")
    return payload


def post_compute_depth_calibration(symbol: str, days: int = 30) -> dict:
    """Compute + persist the empirical depth profile from BV bookDepth daily
    archives (bounded window). Feeds liquidity-floor / slippage models with
    MEASURED venue depth instead of assumptions."""
    from forven.binance_vision import bv_client
    from forven.data import symbol_to_fs
    from forven.db import kv_set_best_effort

    fs_symbol = symbol_to_fs(symbol)
    bounded_days = max(1, min(int(days or 30), 90))
    artifact = bv_client.sample_depth_calibration(fs_symbol, days=bounded_days)
    if artifact is None:
        raise HTTPException(status_code=404, detail=f"no bookDepth archives for {fs_symbol}")
    kv_set_best_effort(f"{_DEPTH_CALIBRATION_KV_PREFIX}{fs_symbol}", artifact)
    _log_data_action_safe(
        "depth_calibration",
        f"Computed depth calibration for {fs_symbol} over {artifact.get('sampled_days')} days",
        symbol=fs_symbol,
    )
    return artifact


def _log_data_action_safe(action: str, message: str, **detail) -> None:
    try:
        from forven.data import _log_data_action

        _log_data_action(action, message, **detail)
    except Exception:
        pass


_universe_lock = threading.Lock()
_universe_cancel = threading.Event()
_UNIVERSE_STATE_KV_KEY = "data:universe_seed_state"
_universe_state: dict = {
    "running": False,
    "last_started_at": None,
    "last_result": None,
    "last_error": None,
    "progress": None,
}
_universe_state_loaded = False


def _load_universe_state_locked() -> None:
    """Seed last_started/result/error from KV once per process. A seed that was
    RUNNING when the process died is surfaced as failed ('backend restarted')
    instead of silently blanking — the seed is resumable, so restarting it is
    always safe. Caller holds _universe_lock."""
    global _universe_state_loaded
    if _universe_state_loaded:
        return
    _universe_state_loaded = True
    try:
        from forven.db import kv_get

        saved = kv_get(_UNIVERSE_STATE_KV_KEY, None)
        if isinstance(saved, dict):
            for key in ("last_started_at", "last_result", "last_error"):
                if _universe_state.get(key) is None:
                    _universe_state[key] = saved.get(key)
            if saved.get("running") and not _universe_state.get("last_error"):
                _universe_state["last_error"] = "backend restarted mid-seed — restart the seed (it resumes)"
    except Exception:
        pass


def _persist_universe_state_locked() -> None:
    try:
        from forven.db import kv_set_best_effort

        kv_set_best_effort(
            _UNIVERSE_STATE_KV_KEY,
            {
                "running": bool(_universe_state.get("running")),
                "last_started_at": _universe_state.get("last_started_at"),
                "last_result": _universe_state.get("last_result"),
                "last_error": _universe_state.get("last_error"),
            },
        )
    except Exception:
        pass


def get_data_universe() -> dict:
    """Symbol registry (inception/delist/liquidity) + the planned research
    universe ladder + seed job state."""
    from forven.dataeng.universe import get_symbol_registry, plan_research_universe

    try:
        registry = get_symbol_registry()
    except Exception as exc:
        log.warning("get_data_universe registry read failed: %s", exc)
        registry = []
    try:
        plan = plan_research_universe()
    except Exception as exc:
        log.warning("get_data_universe plan failed: %s", exc)
        plan = []
    with _universe_lock:
        _load_universe_state_locked()
        seed_state = dict(_universe_state)
    config: dict = {}
    try:
        from forven.dataeng.settings import load_data_engine_settings

        raw = load_data_engine_settings().research_universe
        config = dict(raw) if isinstance(raw, dict) else {}
    except Exception:
        config = {}
    return {
        "registry_count": len(registry),
        "active": sum(1 for row in registry if row.get("status") == "active"),
        "delisted": sum(1 for row in registry if row.get("status") == "delisted"),
        "registry": registry,
        "plan": plan,
        "seed": seed_state,
        "config": config,
    }


def post_universe_config(payload: dict) -> dict:
    """Update the research-universe sizing config (Settings-grade knobs surfaced
    next to the Seed button: presets are premades, every number stays editable).
    Only known keys are accepted; the seed itself remains strictly manual."""
    from forven.dataeng.settings import load_data_engine_settings, save_data_engine_settings

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="config payload must be an object")

    settings = load_data_engine_settings()
    config = dict(settings.research_universe) if isinstance(settings.research_universe, dict) else {}

    if "enabled" in payload:
        config["enabled"] = bool(payload["enabled"])
    for key, lo, hi in (
        ("size", 1, 500),
        ("intraday_top", 0, 500),
        ("minute_top", 0, 100),
        ("metrics_days", 0, 3650),
    ):
        if key in payload:
            try:
                value = int(payload[key])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"{key} must be an integer")
            if not (lo <= value <= hi):
                raise HTTPException(status_code=400, detail=f"{key} must be between {lo} and {hi}")
            config[key] = value
    # Tier tops can't exceed the universe size (a 1m tier larger than the plan
    # is meaningless and confuses the estimate).
    config["intraday_top"] = min(int(config.get("intraday_top", 20)), int(config.get("size", 50)))
    config["minute_top"] = min(int(config.get("minute_top", 10)), int(config.get("intraday_top", 20)))

    settings.research_universe = config
    save_data_engine_settings(settings)
    return {"config": config}


def post_refresh_universe_registry() -> dict:
    from forven.dataeng.universe import refresh_symbol_registry

    return refresh_symbol_registry()


def post_seed_research_universe() -> dict:
    """Seed deep history for the research universe in a background thread.
    Idempotent/resumable; cancel via post_cancel_universe_seed."""
    with _universe_lock:
        if _universe_state["running"]:
            raise HTTPException(status_code=409, detail="Universe seed already running")
        _load_universe_state_locked()
        _universe_cancel.clear()
        _universe_state.update(
            {"running": True, "last_started_at": _now(), "last_result": None, "last_error": None, "progress": None}
        )
        _persist_universe_state_locked()

    def _on_progress(done: int, total: int, symbol: str) -> None:
        with _universe_lock:
            _universe_state["progress"] = {"done": int(done), "total": int(total), "current_symbol": symbol}

    def _run() -> None:
        try:
            from forven.dataeng.universe import seed_research_universe

            result = seed_research_universe(progress_cb=_on_progress, cancel_event=_universe_cancel)
            with _universe_lock:
                _universe_state.update({"running": False, "last_result": result, "progress": None})
                _persist_universe_state_locked()
        except Exception as exc:
            log.warning("Research universe seed failed: %s", exc)
            with _universe_lock:
                _universe_state.update({"running": False, "last_error": str(exc), "progress": None})
                _persist_universe_state_locked()

    threading.Thread(target=_run, daemon=True, name="universe-seed").start()
    return {"status": "started"}


def post_cancel_universe_seed() -> dict:
    with _universe_lock:
        if not _universe_state["running"]:
            raise HTTPException(status_code=409, detail="No universe seed running")
        _universe_cancel.set()
    return {"status": "cancelling"}


def get_coverage() -> dict:
    """Return row counts and date ranges per symbol per stream.

    Scans data/ohlcv/, data/funding/, data/oi/ directories.
    Missing parquet files are omitted from the result.
    """
    from forven.data import DATA_DIR, coverage_entry, prune_coverage_cache
    from forven.data_manager import BASIS_DIR, FUNDING_DIR, OI_DIR

    result: dict = {}
    ohlcv_root = Path(DATA_DIR)

    if not ohlcv_root.exists():
        return result

    visited: set[str] = set()

    def _entry_for(path: Path) -> dict | None:
        visited.add(str(path))
        return coverage_entry(path)

    for sym_dir in sorted(ohlcv_root.iterdir()):
        if not sym_dir.is_dir() or sym_dir.name.startswith("."):
            continue
        symbol = sym_dir.name
        result[symbol] = {}

        # OHLCV timeframes
        for pq_file in sorted(sym_dir.glob("*.parquet")):
            entry = _entry_for(pq_file)
            if entry is not None:
                result[symbol][f"ohlcv/{pq_file.stem}"] = entry

        # Funding
        funding_path = FUNDING_DIR / symbol / "history.parquet"
        if funding_path.exists():
            entry = _entry_for(funding_path)
            if entry is not None:
                result[symbol]["funding"] = entry

        # OI timeframes
        oi_sym_dir = OI_DIR / symbol
        if oi_sym_dir.exists():
            for pq_file in sorted(oi_sym_dir.glob("*.parquet")):
                entry = _entry_for(pq_file)
                if entry is not None:
                    result[symbol][f"oi/{pq_file.stem}"] = entry

        # Basis (perp premium index) timeframes
        basis_sym_dir = BASIS_DIR / symbol
        if basis_sym_dir.exists():
            for pq_file in sorted(basis_sym_dir.glob("*.parquet")):
                entry = _entry_for(pq_file)
                if entry is not None:
                    result[symbol][f"basis/{pq_file.stem}"] = entry

        if not result[symbol]:
            del result[symbol]

    # Keep the per-file cache bounded to series that still exist (delistings,
    # deletes and re-uploads otherwise leak entries in the long-lived worker).
    prune_coverage_cache(visited)

    return result


__all__ = [
    "delete_dataset_stub",
    "get_active_symbols_with_reasons",
    "get_data_engine_status",
    "get_data_health",
    "get_data_ingestion_run",
    "get_data_ingestion_runs",
    "get_data_quality",
    "get_quality_reports",
    "get_data_sources_stub",
    "get_dataset_detail_stub",
    "get_dataset_export",
    "get_dataset_ohlcv",
    "get_datasets_stub",
    "get_ohlcv",
    "get_source_symbols_stub",
    "get_sources_stub",
    "get_stream_health",
    "get_symbol_export",
    "get_symbols_stub",
    "get_backfill_status",
    "get_coverage",
    "post_collect_stream",
    "post_data_engine_backfill_plan",
    "post_data_ingestion_submit",
    "post_trigger_backfill",
    "post_fetch_data",
    "post_upload_csv",
    "post_upload_csv_preview",
    "search_source_symbols_stub",
]
