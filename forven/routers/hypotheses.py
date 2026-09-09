from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from forven.api_domains import hypotheses as hypotheses_domain
from forven.api_security import require_operator_access
from forven.crucible_intake import execute_intake

router = APIRouter(tags=["hypotheses"], dependencies=[Depends(require_operator_access)])
data_gap_router = APIRouter(tags=["hypotheses"], dependencies=[Depends(require_operator_access)])


class HypothesisBulkMutationRequest(BaseModel):
    ids: list[str]


class HypothesisFromUrlPreviewRequest(BaseModel):
    url: str


class HypothesisCreateFromUrlRequest(BaseModel):
    request_id: str | None = Field(default=None, min_length=8, max_length=100)
    url: str
    title: str | None = None
    market_thesis: str | None = None
    mechanism: str | None = None
    claimed_edge: str | None = None


class HypothesisCreateFromUrlsRequest(BaseModel):
    request_id: str | None = Field(default=None, min_length=8, max_length=100)
    urls: list[str]
    title: str | None = None
    market_thesis: str | None = None
    mechanism: str | None = None
    claimed_edge: str | None = None


class HypothesisCreateManualRequest(BaseModel):
    request_id: str | None = Field(default=None, min_length=8, max_length=100)
    title: str
    market_thesis: str
    mechanism: str
    why_now: str | None = None
    target_assets: list[str] | None = None
    target_timeframes: list[str] | None = None
    novelty_score: float | None = Field(default=None, ge=0, le=1)
    claimed_edge: str | None = None
    operator_notes: str | None = None


class HypothesisUpdateRequest(BaseModel):
    title: str | None = None
    market_thesis: str | None = None
    mechanism: str | None = None
    why_now: str | None = None
    target_assets: list[str] | None = None
    target_timeframes: list[str] | None = None
    novelty_score: float | None = Field(default=None, ge=0, le=1)
    operator_notes: str | None = None


class HypothesisGenerateStrategiesRequest(BaseModel):
    force: bool = False


class HypothesisReopenRequest(BaseModel):
    rationale: str | None = None


@router.get("/api/hypotheses")
def list_hypotheses_endpoint(
    view: str | None = None,
    lane: str | None = None,
    status: str | None = None,
    source_type: str | None = None,
    search: str | None = None,
    sort: str | None = None,
    quality: str | None = None,
    include_disproven: bool = False,
    limit: int | None = None,
    offset: int = 0,
):
    # Backward-compatible: when limit/offset are both omitted the response is
    # exactly {"hypotheses": [...]} as before. When limit or offset is supplied,
    # the server slices the page and additionally returns `total`/`limit`/`offset`.
    if limit is None and not offset:
        return {
            "hypotheses": hypotheses_domain.list_hypotheses_summary(
                view=view,
                lane=lane,
                status=status,
                source_type=source_type,
                search=search,
                sort=sort,
                quality=quality,
                include_disproven=include_disproven,
            )
        }
    return hypotheses_domain.list_hypotheses_page(
        view=view,
        lane=lane,
        status=status,
        source_type=source_type,
        search=search,
        sort=sort,
        quality=quality,
        include_disproven=include_disproven,
        limit=limit,
        offset=offset,
    )


@router.get("/api/hypotheses/counts")
def hypotheses_bucket_counts_endpoint():
    """Lightweight per-bucket counts (active/archived/trash/graduated).

    Replaces four full-list fetches the UI previously made just to read lengths.
    """
    return {"counts": hypotheses_domain.get_hypothesis_bucket_counts()}


@router.get("/api/hypotheses/allocator")
def hypotheses_allocator_overview_endpoint(limit: int = 40):
    """CRUX-1 allocator view: daily develop budget, short quota, and the
    active pool ranked by value score — the Crucibles page intelligence strip."""
    from forven.crucible_allocator import allocator_overview

    return allocator_overview(limit=limit)


@router.post("/api/hypotheses/preview_url")
def preview_hypothesis_from_url_endpoint(payload: HypothesisFromUrlPreviewRequest):
    return hypotheses_domain.preview_hypothesis_from_url_payload(payload.url)


@router.post("/api/hypotheses/from_url")
def create_hypothesis_from_url_endpoint(payload: HypothesisCreateFromUrlRequest):
    return execute_intake("url", payload.model_dump(), lambda: hypotheses_domain.create_hypothesis_from_url_payload(
        url=payload.url,
        title=payload.title,
        market_thesis=payload.market_thesis,
        mechanism=payload.mechanism,
        claimed_edge=payload.claimed_edge,
    ))


@router.post("/api/hypotheses/from_urls")
def create_hypothesis_from_urls_endpoint(payload: HypothesisCreateFromUrlsRequest):
    return execute_intake("urls", payload.model_dump(), lambda: hypotheses_domain.create_hypothesis_from_urls_payload(
        urls=payload.urls,
        title=payload.title,
        market_thesis=payload.market_thesis,
        mechanism=payload.mechanism,
        claimed_edge=payload.claimed_edge,
    ))


@router.post("/api/hypotheses/manual")
def create_hypothesis_manual_endpoint(payload: HypothesisCreateManualRequest):
    return execute_intake("manual", payload.model_dump(), lambda: hypotheses_domain.create_hypothesis_manual_payload(
        title=payload.title,
        market_thesis=payload.market_thesis,
        mechanism=payload.mechanism,
        why_now=payload.why_now,
        target_assets=payload.target_assets,
        target_timeframes=payload.target_timeframes,
        novelty_score=payload.novelty_score,
        claimed_edge=payload.claimed_edge,
        operator_notes=payload.operator_notes,
    ))


@router.post("/api/hypotheses/bulk/archive")
def bulk_archive_hypotheses_endpoint(payload: HypothesisBulkMutationRequest):
    return hypotheses_domain.bulk_archive_hypotheses_payload(payload.ids)


@router.post("/api/hypotheses/bulk/trash")
def bulk_trash_hypotheses_endpoint(payload: HypothesisBulkMutationRequest):
    return hypotheses_domain.bulk_trash_hypotheses_payload(payload.ids)


@router.post("/api/hypotheses/bulk/restore")
def bulk_restore_hypotheses_endpoint(payload: HypothesisBulkMutationRequest):
    return hypotheses_domain.bulk_restore_hypotheses_payload(payload.ids)


@router.post("/api/hypotheses/discover")
def trigger_crucible_discovery_endpoint():
    """Operator-triggered external-source crucible discovery (Harvest)."""
    return hypotheses_domain.trigger_crucible_discovery_payload()


@router.get("/api/hypotheses/{hypothesis_id}")
def get_hypothesis_endpoint(hypothesis_id: str, include: str | None = None):
    include_parts = {p.strip().lower() for p in (include or "").split(",") if p.strip()}
    return hypotheses_domain.get_hypothesis_detail_payload(
        hypothesis_id,
        include_content="content" in include_parts,
    )


@router.get("/api/hypotheses/{hypothesis_id}/readiness")
def get_hypothesis_readiness(hypothesis_id: str) -> dict:
    return hypotheses_domain.hypothesis_readiness_payload(hypothesis_id)


@router.post("/api/hypotheses/{hypothesis_id}/archive")
def archive_hypothesis_endpoint(hypothesis_id: str):
    return hypotheses_domain.archive_hypothesis_payload(hypothesis_id)


@router.post("/api/hypotheses/{hypothesis_id}/trash")
def trash_hypothesis_endpoint(hypothesis_id: str):
    return hypotheses_domain.trash_hypothesis_payload(hypothesis_id)


@router.post("/api/hypotheses/{hypothesis_id}/restore")
def restore_hypothesis_endpoint(hypothesis_id: str):
    return hypotheses_domain.restore_hypothesis_payload(hypothesis_id)


@router.post("/api/hypotheses/{hypothesis_id}/reopen")
def reopen_hypothesis_endpoint(
    hypothesis_id: str,
    payload: HypothesisReopenRequest = HypothesisReopenRequest(),
):
    return hypotheses_domain.reopen_hypothesis_payload(
        hypothesis_id, rationale=payload.rationale
    )


@router.post("/api/hypotheses/{hypothesis_id}/verdict")
def trigger_verdict_endpoint(hypothesis_id: str):
    return hypotheses_domain.trigger_verdict_payload(hypothesis_id)


@router.post("/api/hypotheses/{hypothesis_id}/revisit")
def force_revisit_endpoint(hypothesis_id: str):
    return hypotheses_domain.force_revisit_payload(hypothesis_id)


@router.post("/api/hypotheses/cleanup/evidence")
def cleanup_evidence_endpoint(dry_run: bool = False):
    return hypotheses_domain.cleanup_evidence_payload(dry_run=dry_run)


@router.post("/api/hypotheses/cleanup/triage/start")
def cleanup_triage_endpoint(batch_size: int = 10):
    return hypotheses_domain.cleanup_triage_payload(batch_size=batch_size)


@router.post("/api/hypotheses/{hypothesis_id}/update")
def update_hypothesis_endpoint(hypothesis_id: str, payload: HypothesisUpdateRequest):
    return hypotheses_domain.update_hypothesis_payload(
        hypothesis_id,
        title=payload.title,
        market_thesis=payload.market_thesis,
        mechanism=payload.mechanism,
        why_now=payload.why_now,
        target_assets=payload.target_assets,
        target_timeframes=payload.target_timeframes,
        novelty_score=payload.novelty_score,
        operator_notes=payload.operator_notes,
    )


@router.post("/api/hypotheses/{hypothesis_id}/research")
def retrigger_research_endpoint(hypothesis_id: str):
    return hypotheses_domain.retrigger_research_payload(hypothesis_id)


@router.post("/api/hypotheses/{hypothesis_id}/generate-strategies")
def generate_strategies_endpoint(
    hypothesis_id: str,
    payload: HypothesisGenerateStrategiesRequest | None = None,
):
    force = bool(payload.force) if payload is not None else False
    return hypotheses_domain.generate_strategies_payload(hypothesis_id, force=force)


@data_gap_router.get("/api/data-gaps")
def list_ranked_data_gaps_endpoint(limit: int = 20):
    return hypotheses_domain.get_ranked_data_gap_payload(limit=limit)
