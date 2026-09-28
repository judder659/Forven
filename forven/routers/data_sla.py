"""Data Manager routes — Freshness: the SLA census, collector status, venue health, refresh and freeze.

Wire shapes: frontend/src/lib/api/dataManagerTypes.ts. Ownership and semantics:
docs/data-manager-next/CONTRACT.md (workstream B). Keep endpoints thin;
logic lives in forven/dataeng/ or forven/api_domains/.
"""

from fastapi import APIRouter, Depends

from forven.api_security import require_operator_access

router = APIRouter(tags=["data"], dependencies=[Depends(require_operator_access)])
