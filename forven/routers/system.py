from fastapi import APIRouter, Depends, Response

from forven import api_core as core
from forven.api_security import require_operator_access
from forven.risk_effective import with_effective_risk

router = APIRouter(tags=["system"], dependencies=[Depends(require_operator_access)])


@router.post("/api/brain/chat", status_code=202)
def post_brain_chat(body: core.BrainChatBody):
    return core.post_brain_chat(body)


@router.post("/api/brain/chat/direct")
async def post_brain_chat_direct(body: core.BrainChatBody):
    return await core.post_brain_chat_direct(body)


@router.get("/api/brain/chat/{task_id}")
def get_brain_chat_result(task_id: int, response: Response):
    payload = core.get_brain_chat_result(task_id)
    if payload.get("status") in {"pending", "running"}:
        response.status_code = 202
    return payload


@router.get("/api/settings/pipeline")
def get_pipeline_settings():
    return core.get_pipeline_settings()


@router.put("/api/settings/pipeline")
def put_pipeline_settings(body: core.PipelineSettingsUpdateBody):
    return core.put_pipeline_settings(body)


@router.get("/api/settings")
def get_settings():
    return with_effective_risk(core.get_settings())


@router.put("/api/settings/{section}")
def put_settings_section(section: str, payload: dict):
    # API-02: the body stays an open dict on purpose — each section has its own
    # key set and full editability of every gate threshold is a deliberate
    # project stance. The schema lives with the handlers instead:
    # api_core._validate_settings_section_payload rejects unknown keys and
    # out-of-range real-money limits with a 422 BEFORE anything is persisted.
    return core.put_settings_section(section, payload)


@router.get("/api/settings/manifest-defaults")
def get_settings_manifest_defaults():
    # ARCH-05: the Settings page used to hand-copy ~214 engine defaults into
    # frontend/src/lib/settings/manifest.ts, and they drifted — nine gate
    # thresholds were captioning numbers the engine did not run, on the page an
    # operator reviews before arming live capital. The flattening lives in
    # forven/settings_manifest.py, not here: routers stay thin.
    from forven import settings_manifest

    return settings_manifest.manifest_defaults_payload()


@router.post("/api/settings/test-discord")
def post_settings_test_discord():
    return core.post_settings_test_discord()


@router.get("/api/settings/discord-audit")
def get_settings_discord_audit(send_probe: bool = False):
    return core.get_settings_discord_audit(send_probe=send_probe)


@router.post("/api/settings/reset")
def post_settings_reset():
    return core.post_settings_reset()


@router.get("/api/settings/audit-log")
def get_settings_audit_log_route(limit: int = 5):
    return core.get_settings_audit_log(limit=limit)


@router.post("/api/settings/test-remote-engine")
def post_settings_test_remote_engine(body: core.SettingsTestRemoteEngineBody):
    return core.post_settings_test_remote_engine(body)


@router.get("/api/pipeline/thresholds")
def get_pipeline_config():
    return core.get_pipeline_config()


@router.post("/api/pipeline/thresholds")
def update_pipeline_config(config: dict):
    return core.update_pipeline_config(config)


@router.get("/api/pipeline/motion-log")
def get_pipeline_motion_log(limit: int = 200):
    return core.get_pipeline_motion_log(limit=limit)
