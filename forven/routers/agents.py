from fastapi import APIRouter, Depends

from forven import api_core as core
from forven.api_security import require_operator_access
from forven.control_plane import ops as control_plane_ops
from forven.control_plane.models import QueueProcessingBody

router = APIRouter(tags=["agents"], dependencies=[Depends(require_operator_access)])


@router.get("/api/agents")
def read_agents(enabled_only: bool = False):
    return core.read_agents(enabled_only=enabled_only)


@router.get("/api/agents/model-options")
def get_agent_model_options(refresh: bool = False):
    return core.get_agent_model_options(refresh=refresh)


@router.get("/api/model-policy")
def get_model_policy():
    return core.get_model_policy()


@router.put("/api/model-policy")
def put_model_policy(body: core.ModelPolicyUpdateBody):
    return core.put_model_policy(body)


@router.get("/api/agents/provider-health")
def get_agent_provider_health():
    """Provider health: static config-drift warnings + runtime call outcomes.

    ``warnings`` — enabled agents whose configured provider has no credentials
    (static). ``runtime`` — what providers actually did at call time
    (rate_limit/quota/auth/transient/fallback), so the UI fails loudly when a
    connected provider degrades or a call falls back.
    """
    from forven.agents.provider_health import list_agent_provider_warnings
    from forven.provider_runtime_health import get_provider_health_runtime

    warnings = list_agent_provider_warnings()
    runtime = get_provider_health_runtime()
    return {"warnings": warnings, "count": len(warnings), "runtime": runtime}


@router.post("/api/agents/reconcile-providers")
def post_reconcile_agent_providers():
    """Repoint credential-less agents to a configured provider (wizard/manual)."""
    from forven.agents.provider_health import reconcile_agent_providers

    return reconcile_agent_providers()


@router.post("/api/agents/strategy-developers")
def post_strategy_developer_agent(body: core.LegacyAgentCreateBody):
    return core.post_strategy_developer_agent(body)


@router.get("/api/agents/spend")
def get_agents_spend(days: int = 30):
    """Per-agent daily spend rollup (survives run-row pruning)."""
    from forven.db import get_agent_spend

    rows = get_agent_spend(days=days)
    totals: dict[str, dict] = {}
    for row in rows:
        agent = totals.setdefault(
            row["agent_id"],
            {"agent_id": row["agent_id"], "tasks": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0},
        )
        agent["tasks"] += int(row["tasks"] or 0)
        agent["cost_usd"] += float(row["cost_usd"] or 0.0)
        agent["input_tokens"] += int(row["input_tokens"] or 0)
        agent["output_tokens"] += int(row["output_tokens"] or 0)
    return {"days": days, "daily": rows, "totals": sorted(totals.values(), key=lambda a: -a["cost_usd"])}


@router.get("/api/agents/{agent_id}")
def get_agent(agent_id: str):
    return core.get_agent(agent_id)


@router.patch("/api/agents/{agent_id}")
def patch_agent(agent_id: str, body: core.LegacyAgentUpdateBody):
    return core.patch_agent(agent_id, body)


@router.delete("/api/agents/{agent_id}")
def delete_agent(agent_id: str):
    return core.delete_agent_row(agent_id)


@router.get("/api/agents/{agent_id}/documents")
def get_agent_documents(agent_id: str):
    return core.get_agent_documents(agent_id)


@router.get("/api/agents/{agent_id}/documents/{document}")
def get_agent_document(agent_id: str, document: str):
    return core.get_agent_document(agent_id, document)


@router.put("/api/agents/{agent_id}/documents/{document}")
def put_agent_document(agent_id: str, document: str, body: core.LegacyAgentDocumentBody):
    return core.put_agent_document(agent_id, document, body)


@router.patch("/api/agents/{agent_id}/model")
def patch_agent_model(agent_id: str, body: core.LegacyAgentModelBody):
    return core.patch_agent_model(agent_id, body)


@router.post("/api/agents/{agent_id}/test-discord")
def post_agent_test_discord(agent_id: str, body: core.AgentDiscordTestBody | None = None):
    return core.post_agent_test_discord(agent_id, body)


@router.get("/api/agents/{agent_id}/terminal")
def get_agent_terminal(agent_id: str):
    return core.get_agent_terminal(agent_id)


@router.post("/api/agent-tasks/process")
async def process_task_queues(body: QueueProcessingBody):
    return await control_plane_ops.process_task_queues(body)
