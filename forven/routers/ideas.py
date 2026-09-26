from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from forven import strategy_creation
from forven.api_security import require_operator_access
from forven.hypotheses import list_ranked_data_gaps

router = APIRouter(tags=["ideas"], dependencies=[Depends(require_operator_access)])


class IdeaSubmitRequest(BaseModel):
    text: str | None = Field(default=None, max_length=20_000)
    title: str | None = Field(default=None, max_length=300)
    market_thesis: str | None = Field(default=None, max_length=5_000)
    mechanism: str | None = Field(default=None, max_length=5_000)
    target_assets: list[str] | None = None
    target_timeframes: list[str] | None = None
    notes: str | None = Field(default=None, max_length=5_000)
    url: str | None = Field(default=None, max_length=2_000)


class IdeaUrlPreviewRequest(BaseModel):
    url: str = Field(max_length=2_000)


@router.post("/api/ideas")
def submit_idea(body: IdeaSubmitRequest) -> dict:
    return strategy_creation.submit_idea(**body.model_dump())


@router.post("/api/ideas/preview_url")
def preview_idea_url(body: IdeaUrlPreviewRequest) -> dict:
    return strategy_creation.preview_url(body.url)


@router.get("/api/ideas/{idea_id}")
def get_idea(idea_id: str) -> dict:
    payload = strategy_creation.idea_payload(idea_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="idea not found")
    return payload


@router.get("/api/data-gaps")
def list_data_gaps(limit: int = 20) -> dict:
    return {"data_gaps": list_ranked_data_gaps(limit=limit)}
