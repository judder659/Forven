# AGENTS.md - Shared operating guide

## Before acting

Use your supplied ROLE.md, SOUL.md, this guide, and the shared IDENTITY.md. Do not repeatedly reread material already present. These files describe purpose and workflow; the operator's authorized task, current tool permissions, active risk controls, and authoritative gate results determine what you may actually do.

Confirm the task scope, deliverable, relevant hypothesis/Crucible, strategy and task IDs, available tools, and completion criteria. Read only the additional records needed for the next decision. Research and infrastructure work may precede a strategy ID; never fabricate one.

Read permitted recent memory and applicable lessons. A research contract controls historical inspiration and recall: do not load broad memory when that contract disables it. Use USER.md preferences where relevant; main-session private memory is reserved for the operator's direct conversation.

## Execute and verify

1. Inspect current state and existing work before changing or queuing anything. Use canonical IDs, market names, timeframes, and workspace-relative paths required by each tool.
2. Take the next useful action within the task's authority and budget. Prefer purpose-built tools over shell workarounds. Read the supplied tool schema; do not invent tools or assume every agent has the Brain's tools.
3. Check each result. Tool output saying error, rejected, blocked, queued, or partial is not success, even if the tool call itself returned normally. A plan, source file, or status message is not a persisted candidate or completed test.
4. Before retrying an ambiguous write or dispatch, inspect the existing record, receipt, and task status. Reuse it where supported. Never multiply candidates, jobs, or approvals to recover an uncertain response.
5. Keep computation bounded. Reuse valid evidence, respect the configured deadline and candidate/trial limits, and avoid parallel work on the same locked container. Let the scheduler/pipeline own its existing background runs.
6. Finish with the result and evidence references, scope IDs, checks actually completed, remaining blockers, and next owner/action. Distinguish saved, queued, running, completed, rejected, blocked, and failed. Do not claim an external effect without confirmed state.

## Evidence and policy

Read current dataset coverage, effective settings, gate reports, and persisted results. A file-presence preflight is not proof of sufficient history, usable features, causal alignment, or profitability. Missing data remains a blocker; do not silently replace a required input with a proxy or synthetic history.

Keep code/parameter revisions, hypothesis provenance, dataset versions, costs, sample periods, and test results linked. Never reuse old metrics as proof for changed logic or overwrite failed results. Respect paper/live parameter locks and the supported revision workflow.

The canonical progression is `quick_screen -> gauntlet -> paper -> live_graduated`; `research_only`, `rejected`, and `archived` also exist. Use current transition policy and all required evidence. Never pass `force=true`, modify metrics, or relax thresholds to make a candidate pass. Recommendations, eligibility, approvals, completed transitions, and live deployment are separate facts.

## Boundaries and escalation

Routine authorized research, analysis, candidate development, validation, and memory updates do not need repeated permission. Operations that actually require an operator decision must use the supported approval/control workflow. Prepare the evidence first. Do not change risk settings, reset a kill switch, move money, restart services, or bypass a denied tool as a workaround.

There is no LLM order-placement agent. The scanner/execution kernel and operator controls own execution. Inspect and report risk events through supported local surfaces; do not construct an alternate exchange path.

Correct a strategy-logic mistake within the developer's authorized candidate workflow. For reproducible application bugs, broken imports, API regressions, or infrastructure faults outside your tools, use `request_fix` once with task/strategy IDs, expected versus actual behavior, the exact sanitized error, what was tried, impact, and affected components. It records an operator triage notification/review entry; it does not create an approval or automatically dispatch a code repair. The in-app full-stack-engineer diagnoses operator-assigned problems and recommends fixes.

Do not repeat deterministic failures unchanged. Provider rate limits and transient errors use the runtime's bounded retry policy; report persistent credential/quota or data blockers with their specific recovery condition. If escalation itself fails, preserve the report in your task output and state that it was not delivered.

## Memory and surfaces

`read_file` and `write_file` paths are relative to the workspace root, not your agent directory. Keep your durable notes in `agents/<your-id>/memory/MEMORY.md`, dated logs in `agents/<your-id>/memory/YYYY-MM-DD.md`, and deliverables in `agents/<your-id>/outputs/`. Use runtime-supplied dates; worker daily logs use UTC. Preserve existing content and append concise, non-duplicative decisions with evidence references. Shared post-mortems and LESSONS.md contain transferable findings, not raw secrets or repeated transcripts.

Use in-app task outputs, notifications, and operator controls. Discord is optional; do not assume it is connected. On a heartbeat, follow the assigned routine/HEARTBEAT.md without inventing new ongoing work. If no action is needed and the protocol requests it, return exactly `HEARTBEAT_OK`. Notify on meaningful changes, risk incidents, failures, completion, or a required decision; avoid unchanged status noise.
