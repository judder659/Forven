from fastapi import APIRouter, Depends

from forven.api_domains import tasks as tasks_domain
from forven.api_security import require_operator_access

router = APIRouter(tags=["tasks"], dependencies=[Depends(require_operator_access)])


@router.get("/api/agent-tasks")
def get_agent_tasks():
    return tasks_domain.get_agent_tasks()


@router.post("/api/agent-tasks/{task_id}/dismiss")
def dismiss_agent_task(task_id: int, body: dict | None = None):
    payload = body or {}
    source = str(payload.get("source") or "agent_tasks").strip()
    note = str(payload.get("note") or "").strip() or None
    return tasks_domain.dismiss_agent_task(task_id=task_id, source=source, note=note)


@router.get("/api/tasks/containers")
def get_task_containers(
    limit: int = 200,
    status: str | None = None,
    agent_id: str | None = None,
    strategy_id: str | None = None,
):
    return tasks_domain.get_task_containers(
        limit=limit,
        status=status,
        agent_id=agent_id,
        strategy_id=strategy_id,
    )


@router.get("/api/tasks/{task_display_id}/audit")
def get_task_container_audit(task_display_id: str):
    return tasks_domain.get_task_container_audit(task_display_id)


@router.get("/api/tasks/{task_display_id}/transcript")
def get_task_transcript(task_display_id: str):
    return tasks_domain.get_task_transcript(task_display_id)


@router.get("/api/pipeline/task-containers")
def get_pipeline_task_containers(
    limit: int = 200,
    status: str | None = None,
    agent_id: str | None = None,
    strategy_id: str | None = None,
):
    return tasks_domain.get_task_containers(
        limit=limit,
        status=status,
        agent_id=agent_id,
        strategy_id=strategy_id,
    )


@router.get("/api/pipeline/tasks/{task_display_id}/audit")
def get_pipeline_task_audit(task_display_id: str):
    return tasks_domain.get_task_container_audit(task_display_id)


@router.get("/api/pipeline/errors")
def get_pipeline_errors(limit: int = 50):
    return tasks_domain.get_pipeline_errors_stub(limit=limit)


@router.get("/api/pipeline/activity")
def get_pipeline_activity(limit: int = 50):
    return tasks_domain.get_pipeline_activity_stub(limit=limit)


@router.post("/api/pipeline/errors/{task_id}/assign")
def assign_pipeline_error(task_id: int, body: dict):
    agent_id = str(body.get("agent_id") or "").strip()
    reason = str(body.get("reason") or "").strip() or None
    return tasks_domain.assign_pipeline_error_stub(task_id=task_id, agent_id=agent_id, reason=reason)


@router.post("/api/pipeline/seed")
def seed_pipeline():
    return tasks_domain.seed_pipeline()
