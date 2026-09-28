"""Data Manager routes — Operations: data jobs, storage inventory, reclaim, trash, safe delete and the Data Log.

Wire shapes: frontend/src/lib/api/dataManagerTypes.ts. Ownership and semantics:
docs/data-manager-next/CONTRACT.md (workstream C). Keep endpoints thin;
logic lives in forven/dataeng/ or forven/api_domains/.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from forven.api_domains import data_ops
from forven.api_security import require_operator_access
from forven.dataeng import datalog, storage

router = APIRouter(tags=["data"], dependencies=[Depends(require_operator_access)])


class SeriesKeyBody(BaseModel):
    symbol: str
    timeframe: str = ""
    stream: str = "ohlcv"
    venue: str = "canonical"


class HistoryExtendBody(BaseModel):
    symbols: list[str] | None = None
    series: list[SeriesKeyBody] | None = None
    streams: list[str] | None = None


class ReclaimBody(BaseModel):
    kind: str
    item_ids: list[str] | Literal["all"]
    confirm: str = ""


class TrashPurgeBody(BaseModel):
    item_ids: list[str] | Literal["all"] = "all"
    confirm: str = ""


class DeleteBody(BaseModel):
    series: list[SeriesKeyBody]
    confirm: str = ""
    override_consumers: bool = False


# ---------------------------------------------------------------- jobs


@router.get("/api/data/jobs")
def list_data_jobs(
    status: str | None = None,
    kind: str | None = None,
    origin: str | None = None,
    routine: bool | None = None,
    symbol: str | None = None,
    since: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    return data_ops.list_data_jobs(
        status=status, kind=kind, origin=origin, routine=routine, symbol=symbol, since=since, limit=limit, offset=offset
    )


@router.get("/api/data/jobs/summary")
def data_jobs_summary():
    return data_ops.data_jobs_summary()


@router.get("/api/data/jobs/{job_id}")
def get_data_job(job_id: str):
    return data_ops.get_data_job(job_id)


@router.post("/api/data/jobs/{job_id}/cancel")
def cancel_data_job(job_id: str):
    return data_ops.cancel_data_job(job_id)


@router.post("/api/data/jobs/{job_id}/retry")
def retry_data_job(job_id: str):
    return data_ops.retry_data_job(job_id)


@router.post("/api/data/history/extend")
def extend_history(body: HistoryExtendBody):
    try:
        job = data_ops.submit_history_extend(
            symbols=body.symbols,
            series=[key.model_dump() for key in body.series or []],
            streams=body.streams,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"job": job}


# ---------------------------------------------------------------- storage & trash


@router.get("/api/data/storage")
def get_storage(refresh: bool = False):
    return storage.inventory(refresh=refresh)


@router.post("/api/data/storage/reclaim")
def reclaim_storage(body: ReclaimBody):
    return data_ops.reclaim(body.kind, body.item_ids, body.confirm)


@router.get("/api/data/trash")
def list_trash():
    return storage.list_trash()


@router.post("/api/data/trash/{trash_id}/restore")
def restore_trash(trash_id: str):
    return data_ops.restore_trash(trash_id)


@router.post("/api/data/trash/purge")
def purge_trash(body: TrashPurgeBody):
    return data_ops.purge_trash(body.item_ids, body.confirm)


# ---------------------------------------------------------------- safe delete


@router.get("/api/data/delete/check")
def delete_check(symbol: str, timeframe: str, stream: str = "ohlcv", venue: str = "canonical"):
    return data_ops.delete_check(symbol, timeframe, stream=stream, venue=venue)


@router.post("/api/data/delete")
def delete_series(body: DeleteBody):
    return data_ops.delete_series(
        [key.model_dump() for key in body.series],
        confirm=body.confirm,
        override_consumers=body.override_consumers,
    )


# ---------------------------------------------------------------- data log


@router.get("/api/data/log")
def get_data_log(
    category: str | None = None,
    level: str | None = None,
    symbol: str | None = None,
    action: str | None = None,
    since: str | None = None,
    until: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    try:
        return datalog.query_log(
            category=category, level=level, symbol=symbol, action=action, since=since, until=until, q=q, limit=limit, offset=offset
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/data/log/export")
def export_data_log(
    category: str | None = None,
    level: str | None = None,
    symbol: str | None = None,
    action: str | None = None,
    since: str | None = None,
    until: str | None = None,
    q: str | None = None,
):
    try:
        text = datalog.export_csv(category=category, level=level, symbol=symbol, action=action, since=since, until=until, q=q)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        content=text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="data-log.csv"'},
    )
