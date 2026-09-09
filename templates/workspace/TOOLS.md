# TOOLS.md - Runtime and tool use

## Local entry points

| Surface | Default address |
| --- | --- |
| Backend API | `http://127.0.0.1:8003` |
| Frontend development server | `http://127.0.0.1:5173` |

Confirm actual runtime status and configured base URL; these addresses are defaults. Forven can run from the packaged Tauri app, `start_all.ps1`/`start_all.sh`, or the Windows launcher and supervisor. Do not assume closing the UI stops backend workers. A launcher control receipt is not a health check, and stopping a process does not close exchange positions.

For Windows launcher behavior see `docs/desktop-launcher.md` in the repository. Use the configured controls only within the operator's authorized task; do not kill arbitrary Python processes or invent OS service commands. The scheduler, agent/Brain workers, data collection, and risk monitoring depend on the running configuration. Discord and standalone daemons are optional.

## Tool scope

The tool list and schemas supplied for the current agent/task are authoritative. Available tools vary by role and context. A name in documentation does not grant access. Report a denied or unavailable operation and use the supported handoff instead of switching task type, shell, or transport to evade it.

- `read_file` reads workspace-relative files. It is not arbitrary repository access.
- `write_file` writes permitted notes/artifacts under the workspace. Preserve existing notes; respect protected files and allowed suffixes.
- `run_code` is a guarded numeric scratchpad, not an application/database/network inspection environment.
- `register_strategy` validates code and registers a candidate linked to a real hypothesis. Confirm the returned container ID; file creation alone is insufficient.
- `request_fix` records a bug report for operator triage. It does not open an approval, dispatch an automatic engineer task, or modify code.

## External development harness

When an operator-authorized development agent needs API access without MCP, use `forven.agent` from the repository. Start with `python -m forven.agent health` and consult `forven/agent/README.md` for current commands and response fields. Use the app's typed clients in frontend code. This harness is a transport to the same API, not a permission or gate bypass.

`python -m forven` launches the CLI. Development API startup uses `python -m uvicorn --app-dir . forven.api:app --host 127.0.0.1 --port 8003`. Do not start a duplicate backend when the configured launcher already owns one.

Keep local notes concise and dated. Never store credentials here or mistake notes for live configuration.
