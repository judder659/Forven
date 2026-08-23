"""Infra hardening regressions (2026-07-25 audit): the launcher/watchdog/CI gates.

These guard five incidents that all share one shape — a supervisor or a gate
that LOOKED like it was working:

* OPS-1  watchdog.ps1 died at `foreach ($pid ...)` ($PID is a PowerShell
         *Constant*), so every check after the bot-restart step silently never
         ran: 22 consecutive cycles on 2026-07-20 logged "Bot unhealthy -
         restarting" and nothing else. 42 minutes of unsupervised trading, and
         the bot was never actually restarted either.
* OPS-2  three health rules keyed on SCHEDULER state (owned by the API process
         by default) but remediated the Discord bot — forever. Attributing the
         fault correctly is NOT a licence to auto-kill the backend: the first
         fix restarted the live trading process after ~4 minutes of staleness,
         off a KV key that is silently dropped under lock contention. The gate
         now needs staleness past 2x the scheduler's own 900s tick watchdog,
         computed the way health_monitor computes it, held for 5 consecutive
         cycles, AND either a failing /api/health probe or direct evidence that
         the critical live execution scanner is overdue. A backend that still
         answers /api/health without that exact evidence is escalated to a
         human, never killed.
* OPS-3  `Start-Process -Redirect*` truncates its target on open, so the only
         record of why the backend died was destroyed by the restart.
* OPS-6  start_all's restart / backoff / permanent-disable decisions existed
         only in a console scrollback — and routing them to Discord on `>=`
         inside a 10s supervisor loop turned one outage into a CRITICAL alert
         every 10 seconds, so they fire on the transition with a dedupe key.
* OPS-10 no rotation or retention on any launcher log (OPS-3's fix removes the
         accidental truncation that used to bound them).
* TEST-3 CI gated on a hand-maintained allowlist covering 8% of test files.
* TEST-9 ruff did not select the async-blocking rules this project keeps
         regressing on, and F841 was a blanket ignore whose count could only go up.

The .ps1 assertions are static because the scripts manage live processes; the
two `pwsh`-gated tests below actually execute the extracted functions.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WATCHDOG = ROOT / "watchdog.ps1"
START_ALL = ROOT / "start_all.ps1"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT = ROOT / "pyproject.toml"
REQUIREMENTS_CI = ROOT / "requirements-ci.txt"
SCHEDULER = ROOT / "forven" / "scheduler.py"
HEALTH_MONITOR = ROOT / "forven" / "health_monitor.py"
API_CORE = ROOT / "forven" / "api_core.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_ps_comments(text: str) -> str:
    """Drop PowerShell comments so a *description* of a banned pattern (these
    fixes are heavily commented) is never mistaken for the pattern itself."""
    out: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            out.append("")
            continue
        single = double = False
        cut = len(line)
        for i, ch in enumerate(line):
            if ch == "'" and not double:
                single = not single
            elif ch == '"' and not single:
                double = not double
            elif ch == "#" and not single and not double:
                cut = i
                break
        out.append(line[:cut])
    return "\n".join(out)


def _powershell() -> str | None:
    """pwsh (or Windows PowerShell) if this machine has one."""
    return shutil.which("pwsh") or shutil.which("powershell")


def _run_powershell(script: str) -> subprocess.CompletedProcess:
    exe = _powershell()
    assert exe is not None
    return subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=120,
    )


# Extracts a single function's source out of a .ps1 via the PowerShell parser and
# defines it in the current session, so a test can exercise the REAL code without
# running the script (which would start/stop live services).
_EXTRACT_FUNCS = """
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$parseErrors = $null
$tokens = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$tokens, [ref]$parseErrors)
if ($parseErrors -and $parseErrors.Count -gt 0) {{
    throw ('parse errors: ' + (($parseErrors | ForEach-Object {{ $_.Message }}) -join '; '))
}}
foreach ($wanted in @({names})) {{
    $found = $ast.FindAll({{
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $wanted
    }}, $true)
    if (-not $found) {{ throw "function $wanted not found" }}
    Invoke-Expression $found[0].Extent.Text
}}
"""


def _extract_prelude(path: Path, *names: str) -> str:
    quoted = ",".join(f"'{n}'" for n in names)
    return _EXTRACT_FUNCS.format(path=str(path).replace("\\", "\\\\"), names=quoted)


# --------------------------------------------------------------------------
# OPS-1 — the constant-variable assignment that killed the whole watchdog run
# --------------------------------------------------------------------------

# $PID/$Host/$PSHome/$ShellId/$PSVersionTable/$Error are Constant or ReadOnly
# automatic variables: assigning to one throws a TERMINATING error, which in a
# script with $ErrorActionPreference='Stop' and no catch kills the entire run.
# ($null is deliberately absent — `$null = ...` is the idiomatic discard.)
_CONSTANT_AUTOMATICS = ("pid", "host", "error", "pshome", "shellid", "psversiontable")
# These are writable but still automatic; binding one as a loop variable shadows
# meaning the reader relies on, so it is banned in a loop header too.
_LOOP_BANNED_AUTOMATICS = _CONSTANT_AUTOMATICS + ("input", "args")


@pytest.mark.parametrize("script", [WATCHDOG, START_ALL], ids=lambda p: p.name)
def test_ops1_powershell_scripts_never_bind_or_assign_automatic_variables(script: Path) -> None:
    text = _strip_ps_comments(_read(script))

    loop_names = "|".join(_LOOP_BANNED_AUTOMATICS)
    loop_hits = [
        m.group(0)
        for m in re.finditer(rf"foreach\s*\(\s*\$({loop_names})\b", text, re.IGNORECASE)
    ]
    assert loop_hits == [], (
        f"{script.name} binds a PowerShell automatic variable as a loop variable "
        f"({loop_hits}); `foreach ($pid ...)` throws 'Cannot overwrite variable PID "
        "because it is read-only or constant' and aborts the run. Use $procId."
    )

    assign_names = "|".join(_CONSTANT_AUTOMATICS)
    assign_hits = [
        m.group(0)
        for m in re.finditer(rf"(?<![\w:])\$({assign_names})\b\s*=(?!=)", text, re.IGNORECASE)
    ]
    assert assign_hits == [], (
        f"{script.name} assigns to a Constant/ReadOnly automatic variable ({assign_hits})."
    )


def test_ops1_watchdog_outer_try_catches_and_still_reports() -> None:
    """A terminating error must be LOGGED and must not swallow the summary."""
    text = _read(WATCHDOG)
    assert re.search(r"^\} catch \{", text, re.M), (
        "watchdog.ps1's script-level try has no catch: a terminating error "
        "(OPS-1 was exactly this) ends the run with no explanation in the log."
    )
    catch_body = text.split("\n} catch {", 1)[1].split("\n} finally {", 1)[0]
    assert "Write-Log" in catch_body, "the aborted cycle must be logged"
    assert "SKIPPED" in catch_body, "the operator must be told later checks did not run"
    assert "Write-WatchdogSummary" in catch_body, "the summary must still run"


@pytest.mark.skipif(_powershell() is None, reason="no pwsh/powershell on this machine")
def test_ops1_stop_bot_processes_runs_to_completion() -> None:
    """Execute the real Stop-BotProcesses: before the fix this threw immediately."""
    script = _extract_prelude(WATCHDOG, "Stop-BotProcesses") + """
function Write-Log { param([string]$m) }
# A PID that cannot exist: the per-PID inner catch handles it, so the function
# must return normally. Pre-OPS-1 the `foreach ($pid ...)` header threw before
# the loop body ever ran.
$null = Stop-BotProcesses -ProcessIds @(999999)
Write-Output 'COMPLETED'
"""
    result = _run_powershell(script)
    assert result.returncode == 0, result.stderr
    assert "COMPLETED" in result.stdout, result.stdout + result.stderr
    assert "read-only or constant" not in (result.stdout + result.stderr)


# --------------------------------------------------------------------------
# OPS-2 — remediate the process that OWNS the state
# --------------------------------------------------------------------------


_STALL_FUNCS = (
    "ConvertTo-NullableInt",
    "Get-SnapshotValue",
    "Resolve-SchedulerStallReason",
    "Resolve-SchedulerStallAction",
)


def _ps_scalar(name: str) -> int:
    """Read a top-level `$Name = <int>` assignment out of watchdog.ps1."""
    match = re.search(rf"^\${name}\s*=\s*(\d+)\s*$", _read(WATCHDOG), re.M)
    assert match is not None, f"${name} is not a top-level integer in watchdog.ps1"
    return int(match.group(1))


def test_ops2_scheduler_health_rules_remediate_the_backend_not_the_bot() -> None:
    text = _read(WATCHDOG)

    # The snapshot must report who owns the runtime loops, defaulting to the API.
    assert "FORVEN_BOT_OWNS_RUNTIME" in text, (
        "the watchdog must know who owns the scheduler before it remediates it"
    )
    assert '"runtime_owner": "bot" if bot_owns_runtime else "api"' in text

    # Each of the three scheduler conditions is still recognised, and they now
    # flow through one decision function instead of an inline `= $true`.
    for condition in (
        "scheduler heartbeat stale",
        "scheduler stuck in 25s timeout loop",
        "scheduler jobs are stuck in hard-timeout state",
    ):
        assert f'return "{condition}"' in text, condition

    assert re.search(
        r"\$backendSchedulerStall = \$schedulerStallReason\s*\n\s*\$backendNeedsRestart = \$true",
        text,
    ), "a confirmed backend scheduler stall on a SICK backend must restart the BACKEND"
    # ...and only from the one branch the decision function sanctions.
    assert text.count("$backendNeedsRestart = $true") == 2, (
        "the only two ways to set $backendNeedsRestart are the probe-failure "
        "counter and the sanctioned 'restart-backend' stall action"
    )


def test_ops2_stall_threshold_exceeds_the_schedulers_own_tick_watchdog() -> None:
    """A stall gate below the scheduler's own tolerance is a false-positive machine.

    scheduler.py bounds a single tick at _SCHEDULER_TICK_WATCHDOG_SECONDS, and
    db_maintenance alone budgets 600s inside one, so the heartbeat can legally be
    that stale. Any EXTERNAL gate must sit above it.
    """
    tick = re.search(
        r"^_SCHEDULER_TICK_WATCHDOG_SECONDS\s*=\s*([\d.]+)", _read(SCHEDULER), re.M
    )
    assert tick is not None, "scheduler.py no longer defines the tick watchdog"
    tick_seconds = float(tick.group(1))

    threshold = _ps_scalar("SchedulerStallThresholdSeconds")
    assert threshold > tick_seconds, (
        f"the watchdog calls the scheduler stalled at {threshold}s while the "
        f"scheduler itself tolerates a {tick_seconds:.0f}s tick — that is the "
        "300s gate whose 2026-07-12 false positives this fix exists for"
    )
    # And the debounce has to be more than the 2-3 cycles those bursts lasted.
    assert _ps_scalar("SchedulerStallCycleLimit") >= 4


def test_ops2_stall_reason_reads_the_health_monitor_heartbeat_not_the_raw_kv_key() -> None:
    """last_successful_tick alone is a known false positive.

    It is written only after a whole tick returns, and via kv_set_best_effort,
    which silently drops the write under SQLite lock contention.
    health_monitor.check_scheduler compensates by taking the max of three keys;
    the watchdog must consume the same computation.
    """
    text = _read(WATCHDOG)
    # The snapshot reads every key health_monitor reads...
    for key in (
        "scheduler:last_successful_tick",
        "scheduler:last_tick_started",
        "scheduler:last_progress_at",
    ):
        assert key in text, key
    assert "MAX(running_since)" in text, (
        "health_monitor also folds a running job's start time into the heartbeat"
    )
    assert "scheduler_heartbeat = newest(" in text

    # ...and the DECISION consumes the folded value, not the raw key.
    decide = _strip_ps_comments(
        text.split("function Resolve-SchedulerStallReason {", 1)[1].split("\n}\n", 1)[0]
    )
    assert 'Get-SnapshotValue $Health "scheduler_heartbeat_age_seconds"' in decide
    # last_success_age_seconds stays in the snapshot for diagnostics, but must
    # never reach a decision — it is the field that gets silently dropped.
    assert "last_success_age_seconds" not in decide, (
        "Resolve-SchedulerStallReason must not read the raw last_successful_tick age"
    )
    assert "$runtimeHealth.last_success_age_seconds" not in text

    # Guard the source of the logic in case health_monitor moves.
    monitor = _read(HEALTH_MONITOR)
    assert "scheduler:last_tick_started" in monitor and "scheduler:last_progress_at" in monitor


def test_ops2_scheduler_guards_do_not_depend_on_the_powershell_host() -> None:
    """`ConvertFrom-Json` yields Int32 on PS 5.1 and Int64 on pwsh 7.

    The original rules type-tested with `-is [int]` (Int32), so under pwsh every
    scheduler condition was dead code — the same script supervised differently
    depending on which host the Scheduled Task happened to launch.
    """
    text = _strip_ps_comments(_read(WATCHDOG))
    for field in (
        "scheduler_heartbeat_age_seconds",
        "last_error_age_seconds",
        "stuck_job_count",
        "hard_timeout_job_count",
        "bot_task_worker_fresh",
        "bot_task_worker_age_seconds",
    ):
        assert f"{field} -is [int]" not in text, field
        assert f"{field} -is [bool]" not in text, field
    assert "function ConvertTo-NullableInt" in _read(WATCHDOG)


@pytest.mark.skipif(_powershell() is None, reason="no pwsh/powershell on this machine")
def test_ops2_the_2026_07_12_transient_stall_bursts_never_restart_the_backend() -> None:
    """Replay the real log pattern through the REAL decision functions.

    .tmp/logs/watchdog.log 2026-07-12 has nine "Bot unhealthy (scheduler
    heartbeat stale)" clusters (07:41+07:43, 07:57+07:59, 12:19+12:21,
    14:33+14:35, 15:43+15:45, 16:53+16:55, 18:01+18:03+18:05, 19:27+19:29,
    20:13+20:15). Every one is 2-3 consecutive cycles, self-resolves, and is
    bracketed by "All services healthy." with no probe failure. Under a naive
    port of that rule to the backend, each becomes Stop-BackendProcesses on the
    live trading process.
    """
    script = _extract_prelude(WATCHDOG, *_STALL_FUNCS) + """
# ~6 minutes of heartbeat staleness — what a heavy tick or a dropped
# best-effort KV write looks like — with a healthy backend and no stuck jobs.
$burst = '{"scheduler_heartbeat_age_seconds": 380, "last_error": "", "last_error_age_seconds": null, "stuck_job_count": 0, "hard_timeout_job_count": 0}' | ConvertFrom-Json
$reason = [string](Resolve-SchedulerStallReason -Health $burst -ThresholdSeconds %(threshold)d)
Write-Output ('REASON=' + $reason + '|')
foreach ($cycle in 1..3) {
    $action = Resolve-SchedulerStallAction -RuntimeOwner 'api' -StallReason $reason `
        -StallCycles $cycle -StallLimit %(limit)d -BackendHealthy $true
    Write-Output ("CYCLE${cycle}=" + $action)
}
""" % {"threshold": _ps_scalar("SchedulerStallThresholdSeconds"), "limit": _ps_scalar("SchedulerStallCycleLimit")}
    result = _run_powershell(script)
    assert result.returncode == 0, result.stderr
    # 380s of staleness is not a stall at all under the new threshold.
    assert "REASON=|" in result.stdout, result.stdout
    for cycle in (1, 2, 3):
        assert f"CYCLE{cycle}=none" in result.stdout, result.stdout
    assert "restart-backend" not in result.stdout, result.stdout


@pytest.mark.skipif(_powershell() is None, reason="no pwsh/powershell on this machine")
def test_ops2_a_healthy_backend_requires_an_overdue_live_scanner_to_restart() -> None:
    """The money-path guard needs direct live-lane evidence before a healthy restart.

    Bouncing it aborts in-flight jobs and open-order bookkeeping — the sibling
    supervisor at start_all.ps1 refuses for the same reason ("A live+listening
    backend that fails a few probes is almost always mid-job"). The one narrow
    exception is a sustained scheduler stall with the critical live execution
    job itself provably overdue.
    """
    script = _extract_prelude(WATCHDOG, *_STALL_FUNCS) + """
# Two HOURS of staleness: as unambiguous as this signal ever gets.
$wedged = '{"scheduler_heartbeat_age_seconds": 7200, "last_error": "", "last_error_age_seconds": null, "stuck_job_count": 0, "hard_timeout_job_count": 0}' | ConvertFrom-Json
$reason = [string](Resolve-SchedulerStallReason -Health $wedged -ThresholdSeconds 1800)
Write-Output ('REASON=' + $reason)
Write-Output ('HEALTHY_NO_EVIDENCE=' + (Resolve-SchedulerStallAction -RuntimeOwner 'api' -StallReason $reason -StallCycles 500 -StallLimit 5 -BackendHealthy $true))
Write-Output ('HEALTHY_SCANNER_BELOW_LIMIT=' + (Resolve-SchedulerStallAction -RuntimeOwner 'api' -StallReason $reason -StallCycles 4 -StallLimit 5 -BackendHealthy $true -CriticalLiveScannerOverdue $true))
Write-Output ('HEALTHY_SCANNER_AT_LIMIT=' + (Resolve-SchedulerStallAction -RuntimeOwner 'api' -StallReason $reason -StallCycles 5 -StallLimit 5 -BackendHealthy $true -CriticalLiveScannerOverdue $true))
Write-Output ('SICK_BELOW_LIMIT=' + (Resolve-SchedulerStallAction -RuntimeOwner 'api' -StallReason $reason -StallCycles 4 -StallLimit 5 -BackendHealthy $false))
Write-Output ('SICK_AT_LIMIT=' + (Resolve-SchedulerStallAction -RuntimeOwner 'api' -StallReason $reason -StallCycles 5 -StallLimit 5 -BackendHealthy $false))
Write-Output ('BOT_OWNS_RUNTIME=' + (Resolve-SchedulerStallAction -RuntimeOwner 'bot' -StallReason $reason -StallCycles 500 -StallLimit 5 -BackendHealthy $false -CriticalLiveScannerOverdue $true))
Write-Output ('NO_REASON=' + (Resolve-SchedulerStallAction -RuntimeOwner 'api' -StallReason '' -StallCycles 500 -StallLimit 5 -BackendHealthy $false))
"""
    result = _run_powershell(script)
    assert result.returncode == 0, result.stderr
    assert "REASON=scheduler heartbeat stale" in result.stdout, result.stdout
    for expected in (
        # A healthy backend without exact live-scanner evidence stays notify-only.
        "HEALTHY_NO_EVIDENCE=notify",
        # Exact evidence still gets the full sustained-stall debounce.
        "HEALTHY_SCANNER_BELOW_LIMIT=observe",
        "HEALTHY_SCANNER_AT_LIMIT=restart-backend",
        # A sick backend still gets the full debounce.
        "SICK_BELOW_LIMIT=observe",
        "SICK_AT_LIMIT=restart-backend",
        # The bot owning the runtime loops is not the backend's problem.
        "BOT_OWNS_RUNTIME=none",
        "NO_REASON=none",
    ):
        assert expected in result.stdout, result.stdout + result.stderr


def test_ops2_the_healthy_stall_path_escalates_to_a_human() -> None:
    """"We chose not to restart" must be distinguishable from "nothing was wrong"."""
    text = _read(WATCHDOG)
    assert 'if ($schedulerStallAction -eq "notify")' in text
    notify = text.split('$schedulerStallAction -eq "notify"', 1)[1].split("} elseif", 1)[0]
    assert "Send-WatchdogNotification" in notify, (
        "a stall the watchdog deliberately refuses to remediate has to reach an operator"
    )
    # Once per episode, not every 120s cycle forever.
    assert "$schedulerStallCycles -eq $schedulerStallLimit" in notify
    assert "Write-LogThrottled" in notify
    assert 'DedupeKey "watchdog:scheduler_stall:backend"' in notify

    helper = text.split("function Send-WatchdogNotification {", 1)[1].split("\n}\n", 1)[0]
    assert "emit_notification" in helper and "system_degraded" in helper
    assert "dedupe_key" in helper
    assert "try {" in helper and "} catch {" in helper, (
        "a broken notification pipeline must not take the watchdog cycle down"
    )


def test_ops2_snapshot_requires_the_exact_live_execution_scanner_to_be_overdue() -> None:
    text = _read(WATCHDOG)
    snapshot = text.split("function Get-RuntimeHealthSnapshot {", 1)[1].split(
        "function Get-BotProcessIds {", 1
    )[0]

    assert "forven-scanner-hourly" in snapshot
    assert "critical_live_scanner_overdue" in snapshot
    assert "critical_live_scanner_due_age_seconds" in snapshot
    assert "critical_scanner_due_age > 300" in snapshot
    assert 'Get-SnapshotValue $runtimeHealth "critical_live_scanner_overdue"' in text


@pytest.mark.skipif(_powershell() is None, reason="no pwsh/powershell on this machine")
def test_ops2_nullable_int_coercion_matches_across_hosts() -> None:
    script = _extract_prelude(WATCHDOG, "ConvertTo-NullableInt") + """
$parsed = '{"age": 59, "big": 7691364, "missing": null}' | ConvertFrom-Json
Write-Output ('AGE=' + (ConvertTo-NullableInt $parsed.age))
Write-Output ('BIG=' + (ConvertTo-NullableInt $parsed.big))
Write-Output ('MISSING=' + ($null -eq (ConvertTo-NullableInt $parsed.missing)))
Write-Output ('BLANK=' + ($null -eq (ConvertTo-NullableInt '  ')))
# The whole point: the guard fires regardless of Int32/Int64 typing.
Write-Output ('OVER300=' + ((ConvertTo-NullableInt $parsed.big) -gt 300))
"""
    result = _run_powershell(script)
    assert result.returncode == 0, result.stderr
    for expected in ("AGE=59", "BIG=7691364", "MISSING=True", "BLANK=True", "OVER300=True"):
        assert expected in result.stdout, result.stdout


def test_ops2_bot_rule_keys_on_bot_owned_state_only() -> None:
    text = _read(WATCHDOG)

    bot_block = text.split("# --- Check Bot ---", 1)[1].split("# --- Check Daemon ---", 1)[0]
    # The bot may only be declared unhealthy from scheduler state when it
    # genuinely owns the scheduler (FORVEN_BOT_OWNS_RUNTIME=1) — that case is
    # preserved, not removed.
    assert '$runtimeOwner -eq "bot"' in bot_block
    assert "bot task-worker heartbeat stale" in bot_block

    # ...and never from scheduler state under the default owner.
    unhealthy_lines = [
        line for line in bot_block.splitlines() if "$botHealthy = $false" in line
    ]
    assert unhealthy_lines, "the bot health rule disappeared entirely"
    guard = bot_block.split("$botHealthy = $false", 1)[0]
    assert '$runtimeOwner -eq "bot"' in guard, (
        "the bot can only be marked unhealthy from scheduler state behind the "
        "runtime-owner guard; otherwise a stalled BACKEND bounces the bot forever"
    )


def test_ops2_bot_task_worker_signal_is_debounced_and_missing_reads_as_unknown() -> None:
    """The bot-owned trigger needs its own grace, and absence is not staleness.

    runtime_worker.get_bot_task_worker_status() seeds the dict with fresh=False
    and returns it UNCHANGED when bot:task_worker:last_seen is missing, so
    trusting `fresh` marks a bot that has never written a heartbeat as sick.
    BOT_TASK_WORKER_STALE_SECONDS is 120 and the watchdog cycle is also 120s, so
    a bot this rule just relaunched had zero grace before the next kill.
    """
    text = _read(WATCHDOG)

    # Snapshot side: a missing heartbeat reports age=None => fresh=None (unknown),
    # never JSON false.
    assert "if raw_age is None:" in text and "bot_worker_fresh = None" in text
    assert '"bot_task_worker_stale_seconds": bot_worker_stale_seconds' in text

    bot_block = text.split("# --- Check Bot ---", 1)[1].split("# --- Check Daemon ---", 1)[0]
    stripped = _strip_ps_comments(bot_block)
    # Decision side: a measured age well past the stale window, for N cycles.
    assert "$botWorkerAge -gt (3 * $botWorkerStaleWindow)" in stripped, (
        "one stale window is one watchdog cycle — that is not a grace period"
    )
    assert "$null -ne $botWorkerAge -and" in stripped, (
        "an absent heartbeat is UNKNOWN, not stale"
    )
    assert "$botWorkerStallCycles -ge $botWorkerStallLimit" in stripped
    assert "$botWorkerStallFile" in stripped, (
        "each watchdog run is a separate process; the counter must persist on disk"
    )
    # The bot restart must clear both counters so the replacement gets full grace.
    restart = stripped.split("Stop-BotProcesses -ProcessIds", 1)[1]
    assert "Remove-Item -Path $botWorkerStallFile" in restart
    assert "Remove-Item -Path $schedulerStallFile" in restart

    # And the scheduler-stall path for the bot uses the CONFIRMED (debounced)
    # signal, not the raw one-cycle reading.
    assert "if ($schedulerStallConfirmed) {" in stripped


# --------------------------------------------------------------------------
# OPS-3 / OPS-10 — keep the crash evidence, then bound it
# --------------------------------------------------------------------------

_REDIRECT_RE = re.compile(
    r"-RedirectStandardOutput\s+(\$\w+)\s+-RedirectStandardError\s+(\$\w+)"
)


def test_ops3_every_watchdog_redirect_site_rotates_the_log_first() -> None:
    text = _read(WATCHDOG)
    sites = list(_REDIRECT_RE.finditer(text))
    assert sites, "expected Start-Process redirect sites in watchdog.ps1"
    for site in sites:
        for var in site.groups():
            rotate = f"Move-LogAside -Path {var} "
            index = text.find(rotate)
            assert 0 <= index < site.start(), (
                f"{var} is handed to -Redirect* (which TRUNCATES on open) without a "
                f"preceding {rotate.strip()}: the crash evidence for the process being "
                "replaced is destroyed by the replacement."
            )


def test_ops3_start_logged_process_renames_instead_of_deleting() -> None:
    text = _read(START_ALL)
    body = text.split("function Start-LoggedProcess {", 1)[1].split("\n}", 1)[0]
    assert "Remove-Item $StdOutPath" not in body, (
        "Start-LoggedProcess used to delete the previous std* logs outright — that "
        "is the crash evidence for the process it is about to replace."
    )
    assert "Remove-Item $StdErrPath" not in body
    assert "Move-LogAside -Path $StdOutPath" in body
    assert "Move-LogAside -Path $StdErrPath" in body


@pytest.mark.parametrize("script", [WATCHDOG, START_ALL], ids=lambda p: p.name)
def test_ops10_rotation_helper_prunes_and_never_touches_the_live_log(script: Path) -> None:
    body = _strip_ps_comments(
        _read(script).split("function Move-LogAside {", 1)[1].split("\n}\n", 1)[0]
    )
    assert "Move-Item" in body, "rotation must RENAME, not delete"
    assert "Select-Object -Skip" in body, (
        "rotation without retention just trades a truncation bug for a disk leak"
    )
    assert "-like" in body and "-Filter" not in body, (
        "Get-ChildItem -Filter uses legacy 8.3 matching where 'x.log.*' also matches "
        "'x.log', which would prune the LIVE log"
    )
    assert '$_.Name -ne $leaf' in body


@pytest.mark.skipif(_powershell() is None, reason="no pwsh/powershell on this machine")
def test_ops10_move_log_aside_keeps_the_newest_n_rotations(tmp_path: Path) -> None:
    """Execute the real helper: rotate 5 times with Keep=3, expect 3 survivors."""
    workdir = str(tmp_path).replace("\\", "\\\\")
    script = _extract_prelude(WATCHDOG, "Move-LogAside") + f"""
$dir = '{workdir}'
$log = Join-Path $dir 'svc.err.log'
1..5 | ForEach-Object {{
    Set-Content -Path $log -Value ('crash ' + $_)
    Start-Sleep -Milliseconds 1100
    Move-LogAside -Path $log -Keep 3
}}
$rotations = @(Get-ChildItem -Path $dir -File | Where-Object {{ $_.Name -like 'svc.err.log.*' }})
Write-Output ('ROTATIONS=' + $rotations.Count)
Write-Output ('LIVE=' + (Test-Path $log))
$newest = $rotations | Sort-Object LastWriteTime -Descending | Select-Object -First 1
Write-Output ('NEWEST=' + (Get-Content $newest.FullName))
"""
    result = _run_powershell(script)
    assert result.returncode == 0, result.stderr
    assert "ROTATIONS=3" in result.stdout, result.stdout
    # The live path is free for the replacement process to create.
    assert "LIVE=False" in result.stdout, result.stdout
    # Evidence survived rotation rather than being truncated away.
    assert "NEWEST=crash 5" in result.stdout, result.stdout


def test_ops10_watchdog_log_is_capped_and_steady_state_noise_is_throttled() -> None:
    text = _read(WATCHDOG)
    assert "$WatchdogLogMaxBytes" in text and "Move-LogAside -Path $LogFile" in text, (
        "watchdog.log has no natural bound and must be capped"
    )
    # The "another owner is active" line fires on EVERY 2-minute cycle while
    # start_all holds the lock — it was the largest single contributor to the file.
    assert "Write-Log (\"Another watchdog owner is active" not in text
    assert 'Write-LogThrottled -Key "owner_active"' in text
    throttle = text.split("function Write-LogThrottled {", 1)[1].split("\n}\n", 1)[0]
    assert "$IntervalSeconds = 3600" in throttle
    # Each cycle is a separate scheduled-task process, so the stamp must persist.
    assert "Set-Content -Path $stampPath" in throttle


# --------------------------------------------------------------------------
# OPS-6 — the supervisor's decisions must outlive the console window
# --------------------------------------------------------------------------


def test_ops6_start_all_mirrors_console_output_into_a_durable_log() -> None:
    text = _read(START_ALL)
    assert 'Join-Path (Join-Path (Join-Path $script:RepoRoot ".tmp") "logs") "start_all.log"' in text

    for fn in ("Write-Info", "Write-WarnMessage"):
        line = next(
            candidate
            for candidate in text.splitlines()
            if candidate.startswith(f"function {fn} ")
        )
        assert "Write-StartAllLog" in line, f"{fn} still writes only to the console"

    body = text.split("function Write-StartAllLog {", 1)[1].split("\n}\n", 1)[0]
    assert "Add-Content" in body
    assert "yyyy-MM-dd HH:mm:ss" in body, "match watchdog.ps1's Write-Log timestamp shape"


def test_ops6_supervisor_giving_up_reaches_a_human() -> None:
    text = _read(START_ALL)
    body = text.split("function Update-ServiceFailure {", 1)[1].split("\n    }\n", 1)[0]

    perma = body.split("$s.permaDisabled = $true", 1)[1]
    assert "Send-SupervisorNotification" in perma, (
        "permaDisabled means the supervisor will never restart the service again; "
        "that must not be a console-only line"
    )

    backoff = body.split("$s.nextAllowedRestart = $now.AddSeconds", 1)[1]
    assert "Send-SupervisorNotification" in backoff, (
        "the rapid-failure backoff leaves the service DOWN for minutes with nobody told"
    )

    notify = text.split("function Send-SupervisorNotification {", 1)[1].split("\n}\n", 1)[0]
    assert "emit_notification" in notify and "system_degraded" in notify
    # Best-effort: a broken notification pipeline must not take the supervisor down.
    assert "try {" in notify and "} catch {" in notify


def test_ops6_rapid_failure_alert_fires_on_the_transition_not_every_cycle() -> None:
    """`-ge` here means a CRITICAL Discord alert every 10 seconds, forever.

    Update-ServiceFailure is called unconditionally on every supervisor
    iteration while a service is down (interval 10s), and $rapidFailureWindow
    resets the counter every 2 minutes, so `>= threshold` re-fires ~8 times per
    window for the whole outage — each one spawning a cold python interpreter
    that imports forven.notifications (DB + config) SYNCHRONOUSLY inside the
    supervisor loop, with system_degraded_to_discord defaulting to True.
    """
    text = _read(START_ALL)
    body = text.split("function Update-ServiceFailure {", 1)[1].split("\n    }\n", 1)[0]

    backoff = body.split("$s.nextAllowedRestart = $now.AddSeconds", 1)[1]
    assert "$s.failures -eq $rapidFailureThreshold" in backoff, (
        "the alert must fire on the TRANSITION into the backed-off state"
    )
    assert "-ge $rapidFailureThreshold" not in backoff, (
        "no >= guard may wrap the notification — that is the every-10s storm"
    )
    # Even the transition re-arms every $rapidFailureWindow; rate-limit on top.
    assert "$s.lastBackoffNotice" in backoff and "$rapidFailureNoticeInterval" in backoff
    assert "lastBackoffNotice  = [DateTime]::MinValue" in text, (
        "the per-service notice stamp must be initialised with the rest of ServiceState"
    )

    # A stable dedupe_key: emit_notification's DEFAULT key is derived from the
    # title, and these titles embed the varying failure COUNT, so every emission
    # looked unique to the 900s system_degraded cooldown.
    assert 'DedupeKey "start_all:backoff:$Name"' in backoff
    assert 'DedupeKey "start_all:perma_disabled:$Name"' in body
    signature = next(
        line for line in text.splitlines() if line.startswith("    param([string]$Title,")
    )
    assert "$DedupeKey" in signature
    assert "dedupe_key=(p.get('dedupe_key') or None)" in text


@pytest.mark.skipif(_powershell() is None, reason="no pwsh/powershell on this machine")
def test_ops6_a_single_outage_produces_a_single_critical_alert() -> None:
    """Execute the real Update-ServiceFailure across a simulated outage.

    24 supervisor iterations = 4 minutes at the 10s loop interval. Pre-fix
    (`-ge $rapidFailureThreshold`) that is 20 CRITICAL Discord notifications,
    each spawning a cold python interpreter inside the loop.
    """
    script = _extract_prelude(START_ALL, "Update-ServiceFailure") + """
$rapidFailureWindow = [TimeSpan]::FromMinutes(2)
$rapidFailureThreshold = 5
$rapidFailureBackoffSeconds = 300
$rapidFailureNoticeInterval = [TimeSpan]::FromSeconds(900)
$script:Notices = 0
$script:ServiceState = @{ backend = @{
    lastRestart = [DateTime]::MinValue
    failures = 0
    unhealthyChecks = 0
    firstFailureInWin = [DateTime]::MinValue
    nextAllowedRestart = [DateTime]::MinValue
    permaDisabled = $false
    disabledReason = $null
    lastBackoffNotice = [DateTime]::MinValue
} }
function Write-WarnMessage { param([string]$m) }
function Send-SupervisorNotification {
    param([string]$Title, [string]$Summary, [string]$Severity = "critical", [string]$DedupeKey)
    $script:Notices += 1
    Write-Output ("DEDUPE=" + $DedupeKey)
}

# The service is down; the supervisor calls this on EVERY iteration.
1..24 | ForEach-Object { Update-ServiceFailure -Name "backend" -ExitCode 1 }
Write-Output ("AFTER_4_MIN=" + $script:Notices)

# Force the $rapidFailureWindow counter reset the real clock would produce, so
# the threshold is crossed a SECOND time in the same outage.
$script:ServiceState["backend"].firstFailureInWin = [DateTime]::Now.AddMinutes(-5)
1..12 | ForEach-Object { Update-ServiceFailure -Name "backend" -ExitCode 1 }
Write-Output ("AFTER_SECOND_WINDOW=" + $script:Notices)

# The backoff itself must still be re-armed on every failure, not just the first.
Write-Output ("BACKOFF_ARMED=" + ($script:ServiceState["backend"].nextAllowedRestart -gt [DateTime]::Now))
"""
    result = _run_powershell(script)
    assert result.returncode == 0, result.stderr
    assert "AFTER_4_MIN=1" in result.stdout, (
        "one outage must produce ONE critical alert, not one per 10s iteration:\\n"
        + result.stdout
    )
    assert "AFTER_SECOND_WINDOW=1" in result.stdout, (
        "the rate limit must survive the 2-minute failure-window reset:\\n" + result.stdout
    )
    assert "DEDUPE=start_all:backoff:backend" in result.stdout, result.stdout
    assert "BACKOFF_ARMED=True" in result.stdout, (
        "suppressing the ALERT must not suppress the backoff itself"
    )


# --------------------------------------------------------------------------
# TEST-3 — CI gates on the whole suite, not a hand-maintained allowlist
# --------------------------------------------------------------------------


def _backend_steps() -> list[dict]:
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load(_read(CI_WORKFLOW))
    return workflow["jobs"]["backend"]["steps"]


def test_test3_ci_runs_the_entire_test_suite() -> None:
    steps = _backend_steps()
    full = [
        step
        for step in steps
        if "-n auto" in str(step.get("run", "")) and "pytest" in str(step.get("run", ""))
    ]
    assert len(full) == 1, (
        "exactly one CI step must run the whole suite; the allowlist covered 44 of "
        "547 test files and 21 newer files (including test_live_risk_clamp.py and "
        "test_propr_venue.py) were never wired in"
    )
    run = str(full[0]["run"])

    # The invariant is TOTAL COVERAGE, not "no filenames appear". The original
    # form asserted the proxy, and it fired when the subprocess-heavy sandbox
    # suites were moved to a serial lane — they are still RUN, just not under
    # xdist, because they flake when competing for cores (test_isolated_per_bar_
    # matches_in_process passes alone in 5m22s and failed once at -n 6). Asserting
    # the proxy would have forced the choice between a flaky required gate and
    # deleting the guard. So: any file the parallel step --ignore's must be picked
    # up by another pytest step, and nothing may be silently dropped.
    ignored = set(re.findall(r"tests/test_\w+\.py", run))
    if ignored:
        assert "--ignore" in run, (
            "the full-suite step names test files but does not --ignore them — that is "
            "the hand-picked allowlist TEST-3 removed, not a serial lane"
        )
        other_runs = " ".join(
            str(step.get("run", ""))
            for step in steps
            if step is not full[0] and "pytest" in str(step.get("run", ""))
        )
        uncovered = sorted(f for f in ignored if f not in other_runs)
        assert not uncovered, (
            f"these files are --ignore'd by the full-suite step and run by NO other "
            f"step, so they are silently ungated: {uncovered}"
        )


def test_test3_new_test_files_are_gated_without_editing_ci() -> None:
    """Files the old allowlist missed are covered now, by construction."""
    workflow_text = _read(CI_WORKFLOW)
    named = set(re.findall(r"tests/(test_\w+\.py)", workflow_text))
    on_disk = {p.name for p in (ROOT / "tests").glob("test_*.py")}
    assert on_disk - named, "sanity: the themed fast-fail lists are not exhaustive"
    # ...which is fine precisely because the gate is the full run.
    assert "python -m pytest -q -n auto" in workflow_text


def test_test3_quarantine_is_explicit_and_empty() -> None:
    steps = _backend_steps()
    run = next(str(s["run"]) for s in steps if "-n auto" in str(s.get("run", "")))
    assert "QUARANTINE=()" in run, (
        "an explicit (empty) --deselect quarantine is the ONLY sanctioned exclusion "
        "mechanism; without it, exclusions drift back into ad-hoc file lists"
    )
    assert "--deselect" in run
    assert "ONLY sanctioned way" in run, "the quarantine rule must be documented in-place"


def test_test3_backend_job_timeout_fits_the_full_suite() -> None:
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load(_read(CI_WORKFLOW))
    # 5,780 tests / 17m30s at -n 6 on the maintainer's box; a 4-vCPU hosted
    # runner is slower, so the old 25-minute ceiling would have timed out.
    assert workflow["jobs"]["backend"]["timeout-minutes"] >= 40


def test_test3_fast_fail_steps_run_before_the_full_suite() -> None:
    steps = _backend_steps()
    names = [str(s.get("name", "")) for s in steps]
    full_index = next(
        i for i, s in enumerate(steps) if "-n auto" in str(s.get("run", ""))
    )
    fast_fail = [i for i, n in enumerate(names) if n.startswith("Fast-fail")]
    assert fast_fail, "the themed steps are the fast-fail ordering and must be kept"
    assert max(fast_fail) < full_index, (
        "a security or capital-integrity regression must fail in ~30s, not ~20 minutes"
    )
    # The two highest-consequence themes go first.
    assert "security" in names[fast_fail[0]]
    assert "capital-integrity" in names[fast_fail[1]]


def test_test3_xdist_is_declared_where_ci_installs_from() -> None:
    assert re.search(r"^pytest-xdist>=", _read(REQUIREMENTS_CI), re.M), (
        "CI installs from requirements-ci.txt; `-n auto` without xdist is an error"
    )


# --------------------------------------------------------------------------
# TEST-9 — ruff selects the async rule, and F841 is a baseline not a blanket
# --------------------------------------------------------------------------


def _ruff_lint_config() -> dict:
    return tomllib.loads(_read(PYPROJECT))["tool"]["ruff"]["lint"]


def test_test9_ruff_selects_the_async_blocking_rules() -> None:
    lint = _ruff_lint_config()
    assert "ASYNC" in lint["select"], (
        "event-loop starvation is this project's most-repeated incident class; the "
        "linter that catches blocking calls in async defs must gate CI"
    )


def test_test9_async_exemptions_are_per_file_and_narrow() -> None:
    lint = _ruff_lint_config()
    per_file = lint["per-file-ignores"]
    # The startup bootstrap sleep is genuinely one-shot; the starvation test
    # blocks the loop on purpose to prove the watchdog sees it.
    assert "ASYNC251" in per_file["forven/api_core.py"]
    assert per_file["tests/test_loop_starvation_fixes.py"] == ["ASYNC251"]

    # ASYNC109 (`timeout=` on an async def) is an API-shape convention, not a
    # blocking-call check. Carried per-file it needed an entry for every module
    # with such a signature, so any NEW one turned CI red for a non-bug — a live
    # hazard while several agents edit in parallel. Ignored project-wide instead.
    assert lint["ignore"] == ["ASYNC109"]
    for path in (
        "forven/agents/mcp_client.py",
        "forven/bot.py",
        "tests/test_daemon_price_freshness.py",
    ):
        assert path not in per_file, f"{path}'s ASYNC109 entry is now redundant"

    # The blocking-call rules the selection exists for are still live everywhere.
    for exempted in per_file.values():
        assert "ASYNC" not in exempted, "no file may opt out of the whole ASYNC set"
        assert "ASYNC110" not in exempted, "busy-wait detection must gate every file"


def test_test9_api_core_blocking_sleep_stays_a_single_known_instance() -> None:
    """Compensating gate for the whole-file ASYNC251 ignore on api_core.py.

    The ignore exists only because the group that added it does not own
    api_core.py; a whole-file exemption in the app's hottest async module would
    otherwise let a NEW blocking call in unseen. Pin the count until someone with
    ownership replaces it with a line-level `# noqa: ASYNC251`.
    """
    result = subprocess.run(
        [
            sys.executable, "-m", "ruff", "check",
            "--no-cache",
            "--select", "ASYNC251",
            # Neutralise the very exemption this test compensates for.
            "--config", "lint.per-file-ignores = {}",
            "--output-format", "concise",
            str(API_CORE),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    hits = [line for line in result.stdout.splitlines() if "ASYNC251" in line]
    assert len(hits) == 1, (
        "forven/api_core.py is exempted from ASYNC251 as a WHOLE FILE for the "
        "one reviewed startup-bootstrap sleep. It now has "
        f"{len(hits)}:\n" + "\n".join(hits) + "\nEither justify and update this "
        "count, or (better) add a line-level `# noqa: ASYNC251` at the sanctioned "
        "sleep and drop the pyproject per-file entry."
    )


def test_test9_ruff_passes_over_the_whole_tree() -> None:
    """The gate CI actually runs (`ruff check forven tests`), run here too.

    The ASYNC/F841 baselines were originally validated against a working tree
    that includes .gitignore'd scratch strategies and excludes sibling agents'
    untracked test files — i.e. not the tree CI lints.
    """
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", "forven", "tests"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_test9_f841_is_a_shrinking_baseline_not_a_blanket_ignore() -> None:
    lint = _ruff_lint_config()
    assert "F841" not in lint.get("ignore", []), (
        "a blanket ignore means the unused-local count can only grow; F841 is now "
        "enabled with a per-file baseline so it can only shrink"
    )
    f841_files = [
        path for path, codes in lint["per-file-ignores"].items() if "F841" in codes
    ]
    assert f841_files, "the baseline must name the files that currently trip F841"
    for path in f841_files:
        assert (ROOT / path).exists(), (
            f"{path} is baselined for F841 but does not exist — delete the stale entry"
        )


def test_test9_async_rule_actually_fires_under_the_project_config(tmp_path: Path) -> None:
    """Functional proof, not just config introspection."""
    offender = tmp_path / "blocking.py"
    offender.write_text(
        "import time\n\n\nasync def sleeper():\n    time.sleep(1)\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable, "-m", "ruff", "check",
            "--no-cache",
            "--config", str(PYPROJECT),
            "--output-format", "concise",
            str(offender),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "ASYNC251" in result.stdout, result.stdout + result.stderr


# --------------------------------------------------------------------------
# Dependency pinning — CI (and a fresh install) must be reproducible
# --------------------------------------------------------------------------

# Every dep whose behaviour reaches real money, the request surface, or the
# data lake. A new upstream major in any of these changes live trading
# behaviour with no diff to review.
_MUST_HAVE_UPPER_BOUND = (
    "ccxt",
    "hyperliquid-python-sdk",
    "eth-account",
    "fastapi",
    "pydantic",
    "uvicorn",
    "duckdb",
    "pyarrow",
    "pandas",
    "numpy",
    "scipy",
)


def _spec_map(specs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for spec in specs:
        name = re.split(r"[<>=!~\[;]", spec, maxsplit=1)[0].strip()
        if name:
            out[name.lower()] = spec
    return out


def test_runtime_critical_dependencies_have_upper_bounds() -> None:
    deps = _spec_map(tomllib.loads(_read(PYPROJECT))["project"]["dependencies"])
    for name in _MUST_HAVE_UPPER_BOUND:
        spec = deps.get(name)
        assert spec is not None, f"{name} is not declared in pyproject dependencies"
        assert "<" in spec, (
            f"{name} has no upper bound ({spec!r}): a new major can change live "
            "trading behaviour between two installs of the same commit"
        )
        assert ">=" in spec, f"{name} lost its lower bound ({spec!r})"


def test_pydantic_is_declared_so_preflight_can_verify_it() -> None:
    deps = _spec_map(tomllib.loads(_read(PYPROJECT))["project"]["dependencies"])
    assert "pydantic" in deps, (
        "pydantic is imported directly by many forven modules; undeclared, "
        "forven.preflight could not catch it missing from a partial venv"
    )


def test_ci_requirements_bounds_match_pyproject() -> None:
    deps = _spec_map(tomllib.loads(_read(PYPROJECT))["project"]["dependencies"])
    ci_lines = [
        line.strip()
        for line in _read(REQUIREMENTS_CI).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    ci_specs = _spec_map(ci_lines)
    for name in _MUST_HAVE_UPPER_BOUND:
        if name not in ci_specs:
            continue
        assert "<" in ci_specs[name], (
            f"{name} is bounded in pyproject but floats in requirements-ci.txt — "
            "CI would not be reproducible"
        )
        # Floors may legitimately differ (CI pulls a newer pandas than the
        # minimum a user install tolerates), but the CEILINGS must agree or CI
        # gates a different stack than the one that ships.
        def ceiling(spec: str) -> list[str]:
            return [c for c in spec.split(",") if c.lstrip().startswith("<")]

        assert ceiling(ci_specs[name]) == ceiling(deps[name]), (
            f"{name}: pyproject says {deps[name]!r}, requirements-ci.txt says "
            f"{ci_specs[name]!r}"
        )
