"""Data Manager routes — Strategy data contracts: readiness reports, value fingerprints and research-vs-execution divergence.

Wire shapes: frontend/src/lib/api/dataManagerTypes.ts. Ownership and semantics:
docs/data-manager-next/CONTRACT.md (workstream E). Keep endpoints thin;
logic lives in forven/dataeng/ or forven/api_domains/.
"""

from fastapi import APIRouter, Depends

from forven.api_security import require_operator_access

router = APIRouter(tags=["data"], dependencies=[Depends(require_operator_access)])
