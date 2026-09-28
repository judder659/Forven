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
        except Exception as exc:
            log.warning("Remote data ingestion failed: %s", exc)
            raise HTTPException(
                status_code=503,
                detail=f"Remote data ingestion failed: {exc}",
            ) from exc
        data = data if isinstance(data, dict) else {}
        # Only what the remote reported: the run is polled through the remote
        # run listing (get_data_ingestion_run), never fabricated locally.
        return {
            "id": data.get("run_id") or data.get("id"),
            "symbol": _to_ui_symbol(symbol),
            "timeframe": timeframe,
            "source": exchange,
            "status": data.get("status") or "pending",
            "bars_fetched": int(data.get("bars_fetched") or 0),
            "bars_new": int(data.get("bars_new") or 0),
            "started_at": data.get("started_at") or datetime.now(timezone.utc).isoformat(),
            "completed_at": data.get("completed_at"),
            "error": data.get("error"),
            "remote": True,
        }

    from forven.data import submit_ingestion

    try:
        return submit_ingestion(
            symbol=symbol,
            timeframe=timeframe,
            exchange=exchange,
            limit=limit if not all_available else None,
            since_ms=since,
            until_ms=until,
            all_available=all_available,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def get_data_ingestion_run(run_id: str):
    # Local runs are download jobs — a keyed lookup in the job store, not the
    # previous linear scan of up to 10k reconstructed rows on every 1.5s poll.
    from forven.data import get_ingestion_run

    run = get_ingestion_run(str(run_id))
    if run is not None:
        run["symbol"] = _to_ui_symbol(run.get("symbol"))
        return run
    # Synthetic catalog ids ("dataset-N-...") and remote runs (remote mode)
    # go through the composite listing.
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
        except Exception as exc:
            log.warning("Remote direct data fetch failed: %s", exc)
            raise HTTPException(
                status_code=503,
                detail=f"Remote direct data fetch failed: {exc}",
            ) from exc
        # The remote's own answer, never invented bar counts or date ranges.
        try:
            remote = resp.json()
        except ValueError:
            remote = None
        if isinstance(remote, dict):
            return remote
        return {"symbol": _to_ui_symbol(symbol), "timeframe": timeframe, "source": exchange, "status": "submitted"}

    from forven.data import LakeVenueRefused, fetch_ohlcv_chunked

    try:
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
    except LakeVenueRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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

    # Moves the series to the Data Manager trash (restorable); 409 when a
    # live/paper/pipeline consumer reads it (the new delete review can override).
    from forven.api_domains.data_ops import legacy_delete

    try:
        legacy_delete(symbol, timeframe)
    except HTTPException:
        raise
    except Exception as exc:
        log.error("Failed to delete dataset %s %s: %s", symbol, timeframe, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
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


# Data-quality leaderboard (the old /data page), served from the Data Manager
# catalog: every canonical OHLCV series, worst first, scored by the one rubric
# in forven/dataeng/quality.py (freshness is the SLA, not part of the score).
# Nothing here loads a series: scores come from the catalog's quality cache,
# which a background pass fills and refreshes when files change.


def _leaderboard_row(row: dict, quality_row: dict, idx: int) -> dict:
    """One ``QualityReport`` row (frontend/src/lib/api/data.ts) from a catalog row."""
    stats = quality_row.get("stats") or {}
    summary = quality_row.get("summary") or {}
    symbol = _to_ui_symbol(row["symbol"])
    first_ms = stats.get("first_ms")
    last_ms = stats.get("last_ms")
    lag = row["sla"].get("lag_seconds")
    return {
        "id": f"quality-{idx}-{symbol}-{row['timeframe']}",
        "symbol": symbol,
        "timeframe": row["timeframe"],
        "row_count": int(stats.get("rows") or row["rows"] or 0),
        "start_ts": row["first_ts"],
        "end_ts": row["last_ts"],
        "duration_days": round(max(0, (last_ms or 0) - (first_ms or 0)) / 86_400_000, 6) if first_ms and last_ms else 0.0,
        "gaps": int(stats.get("missing_bars") or 0),
        "gap_details": [],
        "null_values": int(stats.get("null_rows") or 0),
        "price_range_min": float(stats.get("price_min") or 0.0),
        "price_range_max": float(stats.get("price_max") or 0.0),
        "volume_min": float(stats.get("volume_min") or 0.0),
        "volume_max": float(stats.get("volume_max") or 0.0),
        "volume_avg": float(stats.get("volume_avg") or 0.0),
        "outliers_close": int(stats.get("outliers") or 0),
        "outliers_volume": int(stats.get("volume_outliers") or 0),
        "invalid_high_low": int(stats.get("invalid_high_low") or 0),
        "invalid_close_range": int(stats.get("invalid_range") or 0),
        "freshness_hours": round(float(lag) / 3600.0, 3) if lag is not None else 0.0,
        "is_stale": row["sla"]["state"] in ("late", "breach"),
        "quality_score": summary.get("score"),
        "quality_issues": summary.get("issues") or [],
        "computed_at": quality_row.get("computed_at") or _now(),
    }


def _scored(snapshot, row: dict) -> dict | None:
    from forven.dataeng.catalog_index import quality_summary

    cached = snapshot.quality.get(row["id"])
    if not cached or (cached.get("stats") or {}).get("error"):
        return None
    return {**cached, "summary": quality_summary(cached)}


def get_quality_reports(limit: int = 100) -> list[dict]:
    """Every canonical OHLCV series with a computed quality score, worst first.

    Remote data mode owns its own catalog, so the local lake is not read there:
    an empty list comes back (the UI shows "no reports yet"). Right after a
    backend start the list fills in as the catalog's background pass scores
    series.
    """
    remote_enabled, _ = _remote_data_engine_config()
    if remote_enabled:
        return []

    from forven.dataeng import catalog_index

    snapshot = catalog_index.get_snapshot()
    scored = []
    for row in snapshot.rows:
        if row["stream"] != "ohlcv" or row["venue"] != catalog_index.CANONICAL_VENUE:
            continue
        quality_row = _scored(snapshot, row)
        if quality_row is not None and quality_row["summary"].get("score") is not None:
            scored.append((row, quality_row))
    scored.sort(key=lambda pair: (pair[1]["summary"]["score"], pair[0]["id"]))
    cap = max(1, min(int(limit or 100), 5000))
    return [_leaderboard_row(row, quality_row, idx) for idx, (row, quality_row) in enumerate(scored[:cap])]


def get_quality_report(symbol: str, timeframe: str) -> dict:
    """Single-series quality report in leaderboard row shape, from the catalog;
    a series not scored yet is scored now (and cached)."""
    from forven.data import symbol_to_fs
    from forven.dataeng import catalog_index

    found = catalog_index.find("ohlcv", catalog_index.CANONICAL_VENUE, symbol_to_fs(symbol), str(timeframe))
    if found is None:
        raise HTTPException(status_code=404, detail=f"dataset not found: {symbol_to_fs(symbol)} {timeframe}")
    snapshot, series = found
    row = snapshot.by_id.get(series.id)
    quality_row = _scored(snapshot, row) if row is not None else None
    if row is None or quality_row is None:
        catalog = catalog_index.catalog_for(snapshot.root)
        catalog_index.refresh_quality([series], catalog=catalog, min_interval=0.0)
        snapshot = catalog_index.get_snapshot(root=snapshot.root, force=True)
        row = snapshot.by_id.get(series.id)
        quality_row = _scored(snapshot, row) if row is not None else None
    if row is None or quality_row is None:
        raise HTTPException(status_code=404, detail=f"quality unavailable: {series.symbol} {timeframe}")
    return _leaderboard_row(row, quality_row, 0)


def get_dataset_versions(symbol: str | None = None, timeframe: str | None = None, limit: int = 50) -> list[dict]:
    """Dataset version history — REAL, backed by the point-in-time revision log.

    Rows:
    - one "current" row per series (live snapshot; checksum included only for
      a single-series query — hashing every file on the unfiltered call is a
      full-lake read);
    - one row per RESTATEMENT event (bars superseded at the same observed_at),
      from the append-only revision lake (one DuckDB scan of the logs'
      timestamp/observed_at columns).
    """
    from forven.data import compute_checksum, scan_datasets, symbol_to_fs
    from forven.dataeng.revisions import latest_restatements, revisions_root

    fs_filter = symbol_to_fs(symbol) if symbol else None
    single_series = bool(fs_filter and timeframe)
    capped = max(1, int(limit or 50))
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

    try:
        events = latest_restatements(revisions_root(), symbol=fs_filter, timeframe=timeframe, limit=capped)
    except Exception as exc:
        log.warning("dataset versions: revision logs unreadable: %s", exc)
        events = []
    for event in events:
        rows.append(
            {
                "id": f"rev-{event['symbol']}-{event['timeframe']}-{event['observed_at']}",
                "symbol": _to_ui_symbol(event["symbol"]),
                "timeframe": event["timeframe"],
                "source": "restatement",
                "row_count": event["rows"],
                "start_ts": event["first_ts"],
                "end_ts": event["last_ts"],
                "checksum": None,
                "ingestion_run_id": None,
                "created_at": event["observed_at"],
            }
        )

    rows.sort(key=lambda row: core._to_datetime_sort_key(row.get("created_at")), reverse=True)
    return rows[:capped]


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
    """Chronological log of data actions for the old /data Activity tab:
    ``activity_log`` rows with ``source='data'`` plus the non-routine data
    jobs (downloads, deep history, reclaims, ...). The new page reads the
    categorized Data Log (``/api/data/log``, forven/dataeng/datalog.py)."""
    from forven.dataeng.datalog import legacy_events

    return {"events": legacy_events(int(limit)), "generated_at": _now()}


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
    """The old /data page's backfill plan: the SLA collector's candle queue
    (refreshes, bootstraps and gap repairs, most urgent first) in the plan
    shape the page renders. The collector drains the same queue every tick."""
    from forven.dataeng import collector

    try:
        snapshot = collector.get_snapshot(refresh=True)
        tasks = [t for t in collector.build_queue(snapshot) if t.row.stream == "ohlcv"]
    except Exception as exc:
        log.error("Failed to plan Data Engine backfill: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "task_count": len(tasks),
        "tasks": [collector.legacy_plan_task(task, snapshot.now) for task in tasks],
    }


def post_execute_data_engine_backfill(max_tasks: int = 10) -> dict:
    """The old page's "Execute plan": run the head of the candle queue now as
    one user refresh job (Jobs view / Data Log) and wait for it. Returns the
    old summary shape; a queue item that fetched nothing newer while its
    series was late counts as failed, never as a green success."""
    from forven.dataeng import collector, jobs

    try:
        snapshot = collector.get_snapshot(refresh=True)
        queue = collector.build_queue(snapshot)
        candles = [t for t in queue if t.row.stream == "ohlcv"]
        batch = candles[: max(1, min(int(max_tasks or 10), 50))]
        if not batch:
            return {"planned_total": len(queue), "candle_total": 0, "executed": 0, "rows_added": 0,
                    "failed": 0, "bootstrapped": 0, "deadline_hit": False, "results": []}
        job = collector.submit_refresh(
            series=[t.row.id for t in batch],
            mode="queue",
            title=f"Execute backfill plan ({len(batch)} series)",
        )
        done = jobs.wait_for(job["id"], timeout=600.0) or job
    except Exception as exc:
        log.error("Failed to execute Data Engine backfill: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    outcome = done.get("result") if isinstance(done.get("result"), dict) else {}
    results = []
    for item in outcome.get("results") or []:
        entry = {"symbol": item.get("symbol"), "timeframe": item.get("timeframe"), "rows_added": int(item.get("bars_added") or 0)}
        if item.get("error"):
            entry["error"] = str(item["error"])[:200]
        elif item.get("action") in ("refresh", "gaps") and not item.get("bars_added"):
            entry["stalled"] = True
        results.append(entry)
    failed = sum(1 for r in results if r.get("error") or r.get("stalled"))
    if done.get("status") == "failed" and not results:
        failed = len(batch)
    return {
        "planned_total": len(queue),
        "candle_total": len(candles),
        "executed": len(results) or len(batch),
        "rows_added": int(outcome.get("bars_added") or 0),
        "failed": failed,
        "bootstrapped": sum(1 for r in outcome.get("results") or [] if r.get("action") == "bootstrap"),
        "deadline_hit": done.get("status") in ("queued", "running"),
        "results": results[:50],
        "job_id": done.get("id"),
    }


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
    from forven.data import LakeVenueRefused, process_csv_upload

    try:
        payload = process_csv_upload(
            content=content,
            filename=filename,
            symbol=symbol,
            timeframe=timeframe,
            ts_col=timestamp_column,
            date_format=date_format,
        )
    except ValueError as exc:  # includes acquire.ImportRejected (400 or 409)
        raise HTTPException(status_code=getattr(exc, "status", 400), detail=str(exc)) from exc
    except LakeVenueRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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

def _stream_health_from_footer(row_count: int, last_ms: int | None, timeframe: str, tier: str) -> dict:
    """Stream health from footer stats only — NEVER a full column/series load —
    classified through the freshness SLA at the series' consumer tier:
    "live" = fresh, "accumulating" = late or in breach, "no_data" = missing."""
    from forven.dataeng import sla

    if not row_count or last_ms is None:
        return {"status": "no_data", "row_count": 0, "last_updated": None, "data_age_hours": None}
    last_ts = pd.Timestamp(last_ms, unit="ms", tz="UTC")
    assessment = sla.assess(last_ms, timeframe, tier)
    return {
        "status": "live" if assessment["state"] == "fresh" else "accumulating",
        "row_count": int(row_count),
        "last_updated": last_ts.isoformat(),
        "data_age_hours": round(float(assessment["lag_seconds"] or 0.0) / 3600, 2),
        "sla": assessment,
    }


def _footer_stream_stats(path) -> tuple[int, int | None, int | None]:
    """(row_count, first_ms, last_ms) for a stream parquet from its footer;
    (0, None, None) when absent/unreadable."""
    from forven.data import _footer_bounds

    try:
        if not path.exists():
            return 0, None, None
        return _footer_bounds(path)
    except Exception:
        return 0, None, None


def _funding_cadence(rows: int, first_ms: int | None, last_ms: int | None) -> str:
    """Nearest of 1h/4h/8h to a funding file's average print spacing (the
    same inference the lake enumeration uses)."""
    if rows > 1 and first_ms is not None and last_ms is not None and last_ms > first_ms:
        hours = (last_ms - first_ms) / 3_600_000.0 / (rows - 1)
        return f"{min((1, 4, 8), key=lambda cadence: abs(cadence - hours))}h"
    return "8h"


def get_stream_health(symbol: str) -> dict:
    """Return health for OHLCV, Funding, and OI streams for a symbol."""
    try:
        from forven.data import _series_row_count, dataset_last_timestamp_ms, symbol_to_fs
        from forven.data_manager import FUNDING_DIR, OI_DIR, data_manager
        from forven.dataeng.consumers import get_consumer_index

        fs_symbol = symbol_to_fs(symbol)
        index = get_consumer_index()

        # OHLCV — use most recently active timeframe (footer reads only)
        timeframes = data_manager.get_active_timeframes(symbol)
        tf = next(iter(timeframes)) if timeframes else "1h"
        ohlcv_health = _stream_health_from_footer(
            _series_row_count(symbol, tf),
            dataset_last_timestamp_ms(symbol, tf),
            tf,
            index.for_series(fs_symbol, tf).tier,
        )
        ohlcv_health["timeframe"] = tf

        # Funding
        funding_rows, funding_first, funding_last = _footer_stream_stats(FUNDING_DIR / fs_symbol / "history.parquet")
        funding_health = _stream_health_from_footer(
            funding_rows,
            funding_last,
            _funding_cadence(funding_rows, funding_first, funding_last),
            index.symbol_tier(fs_symbol),
        )

        # OI — first timeframe with data
        oi_rows, oi_last, oi_tf = 0, None, "1h"
        for t in list(timeframes) + ["1h", "4h"]:
            oi_rows, _, oi_last = _footer_stream_stats(OI_DIR / fs_symbol / f"{t}.parquet")
            if oi_rows:
                oi_tf = t
                break
        oi_health = _stream_health_from_footer(oi_rows, oi_last, oi_tf, index.for_series(fs_symbol, oi_tf).tier)

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
            # The SLA collector's tail refresh (bootstraps a series not stored yet).
            from forven.data import symbol_to_fs
            from forven.dataeng import collector

            fs_symbol = symbol_to_fs(symbol)
            rows_added = 0
            for tf in sorted(data_manager.get_active_timeframes(symbol)):
                result = collector.refresh_now(collector.series_id("ohlcv", "canonical", fs_symbol, tf))
                rows_added += int(result.get("bars_added") or 0)
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


# Binance Vision deep history for the old page's Maintenance tab. Runs as a
# history_extend data job (forven/api_domains/data_ops.py); these keep the old
# payloads, derived from the latest job of that kind.


def get_backfill_status() -> dict:
    """``BackfillStatus`` (running, cancel_requested, progress, last result/error)."""
    from forven.api_domains.data_ops import backfill_status

    return backfill_status()


def post_cancel_backfill() -> dict:
    """Cooperative stop of the running deep-history job (between symbols)."""
    from forven.api_domains.data_ops import cancel_backfill

    return cancel_backfill()


def post_trigger_backfill(symbol: str | None = None) -> dict:
    """Start a deep-history job for one symbol or every stored symbol."""
    from forven.api_domains.data_ops import trigger_backfill

    return trigger_backfill(symbol)


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
    from forven.api_domains.data_ops import universe_seed_state

    seed_state = universe_seed_state()  # latest universe_seed job
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
    if "asset_classes" in payload:
        from forven.dataeng.universe import ASSET_CLASSES

        raw = payload["asset_classes"]
        chosen = [str(value).strip().lower() for value in raw] if isinstance(raw, list) else []
        if not chosen or any(value not in ASSET_CLASSES for value in chosen):
            raise HTTPException(status_code=400, detail=f"asset_classes must be a non-empty subset of {list(ASSET_CLASSES)}")
        config["asset_classes"] = [value for value in ASSET_CLASSES if value in chosen]
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
    """Seed deep history for the research universe as a ``universe_seed`` data
    job -> ``UniverseSeedResponse`` (``already_running`` with the active job
    instead of a second one). Resumable: stored series are skipped."""
    from forven.api_domains.data_ops import start_universe_seed

    return start_universe_seed()


def post_cancel_universe_seed() -> dict:
    """Cooperative stop of the running seed (between symbols); 409 when idle."""
    from forven.api_domains.data_ops import cancel_universe_seed

    return cancel_universe_seed()


def get_coverage() -> dict:
    """Row counts and date ranges per symbol per stream (the old coverage
    matrix): canonical OHLCV timeframes plus the symbol's funding, OI and basis
    series, from the lake's cached parquet footers (no tree of full reads).
    Empty series are omitted."""
    from forven.dataeng.catalog_index import lake_root
    from forven.dataeng.lake import CANONICAL_VENUE, enumerate_series

    series = enumerate_series(streams=("ohlcv", "funding", "oi", "basis"), root=lake_root())
    symbols = {item.symbol for item in series if item.stream == "ohlcv" and item.venue == CANONICAL_VENUE}
    result: dict = {}
    for item in series:
        if item.venue != CANONICAL_VENUE or item.symbol not in symbols:
            continue
        if not item.rows or item.first_ms is None or item.last_ms is None:
            continue
        key = "funding" if item.stream == "funding" else f"{item.stream}/{item.timeframe}"
        start_ts = pd.Timestamp(item.first_ms, unit="ms", tz="UTC")
        end_ts = pd.Timestamp(item.last_ms, unit="ms", tz="UTC")
        result.setdefault(item.symbol, {})[key] = {
            "rows": int(item.rows),
            "from": start_ts.strftime("%Y-%m-%d"),
            "to": end_ts.strftime("%Y-%m-%d"),
            # Precise last-bar timestamp so the matrix can compute hour-granular,
            # timeframe-aware freshness.
            "to_ts": end_ts.isoformat().replace("+00:00", "Z"),
        }
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
