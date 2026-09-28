"""Data Manager routes — Getting data in: download targets, estimates, download jobs and file imports.

Wire shapes: frontend/src/lib/api/dataManagerTypes.ts. Ownership and semantics:
docs/data-manager-next/CONTRACT.md (workstream A). Keep endpoints thin;
logic lives in forven/dataeng/ or forven/api_domains/.
"""

from typing import Any, Callable

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from forven.api_security import require_operator_access
from forven.data import LakeShrinkRefused, LakeVenueRefused
from forven.dataeng import acquire
from forven.dataeng.jobs import DiskSpaceError
from forven.routers.data import _read_upload_bounded

router = APIRouter(tags=["data"], dependencies=[Depends(require_operator_access)])


class DownloadItems(BaseModel):
    items: list[dict[str, Any]]


def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except acquire.ImportRejected as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except (LakeVenueRefused, LakeShrinkRefused) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DiskSpaceError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/data/acquire/targets")
def get_acquire_targets(symbol: str):
    """VenueTargetsResponse: where a symbol can be downloaded from and where it would be stored."""
    return _call(acquire.targets, symbol)


@router.post("/api/data/acquire/estimate")
def post_acquire_estimate(body: DownloadItems):
    """DownloadEstimateResponse for the Get-data review step."""
    return _call(acquire.estimate, body.items)


@router.post("/api/data/acquire/downloads")
def post_acquire_downloads(body: DownloadItems):
    """Queue one download job per item: { jobs: DataJob[] }."""
    return {"jobs": _call(acquire.start_downloads, body.items, origin="user")}


@router.post("/api/data/acquire/import/preview")
async def post_acquire_import_preview(
    file: UploadFile = File(...),
    symbol: str | None = Form(None),
    timeframe: str | None = Form(None),
    timestamp_column: str | None = Form(None),
    date_format: str | None = Form(None),
    timezone: str | None = Form(None),
    mapping_json: str | None = Form(None),
):
    """ImportPreview: parsed rows, inferred timeframe and the overlap with the stored series."""
    content = await _read_upload_bounded(file)
    return await run_in_threadpool(
        _call,
        acquire.preview_import,
        content,
        file.filename or "upload.csv",
        symbol=symbol,
        timeframe=timeframe,
        timestamp_column=timestamp_column,
        date_format=date_format,
        timezone=timezone,
        mapping_json=mapping_json,
    )


@router.post("/api/data/acquire/import")
async def post_acquire_import(
    file: UploadFile = File(...),
    symbol: str = Form(...),
    timeframe: str = Form(...),
    mode: str = Form(...),
    conflict_policy: str = Form(...),
    timestamp_column: str | None = Form(None),
    date_format: str | None = Form(None),
    timezone: str | None = Form(None),
    mapping_json: str | None = Form(None),
):
    """ImportResult: bars written into a new or an existing (patch) series."""
    content = await _read_upload_bounded(file)
    return await run_in_threadpool(
        _call,
        acquire.commit_import,
        content,
        file.filename or "upload.csv",
        symbol=symbol,
        timeframe=timeframe,
        mode=mode,
        conflict_policy=conflict_policy,
        timestamp_column=timestamp_column,
        date_format=date_format,
        timezone=timezone,
        mapping_json=mapping_json,
    )
