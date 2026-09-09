"""Read-only, bounded HTTP observation of a running Forven backend.

No Forven imports, database writes, task submission, or trading calls. Records
every sample, including failures; never restarts the service to improve results.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


def summarize(samples: list[dict]) -> dict:
    good = [sample for sample in samples if "health" in sample]
    latencies = sorted(sample["latency_seconds"] for sample in good)
    p95 = latencies[max(0, math.ceil(len(latencies) * .95) - 1)] if latencies else None
    pids = {sample["health"].get("details", {}).get("api_task_worker", {}).get("pid") for sample in good}
    violations: dict[str, int] = {}
    for sample in samples:
        for violation in sample.get("violations", []):
            violations[violation] = violations.get(violation, 0) + 1
    return {
        "ok": bool(good) and not violations and len(pids) == 1 and None not in pids,
        "complete": False,
        "samples": len(samples), "successful_requests": len(good),
        "pids": sorted(pid for pid in pids if pid is not None),
        "p95_latency_seconds": p95, "max_latency_seconds": max(latencies) if latencies else None,
        "violations": violations,
        "started_at": samples[0]["at"] if samples else None,
        "ended_at": samples[-1]["at"] if samples else None,
    }


def assess(health: dict, latency: float) -> list[str]:
    issues = []
    detail = health.get("details") or {}
    if latency > 5:
        issues.append("request_over_5_seconds")
    threads = detail.get("runtime_threads") or []
    required = {"forven-scheduler-loop", "forven-headless-agent-loop", "forven-headless-brain-loop"}
    alive = {t.get("name") for t in threads if t.get("alive")}
    if not required <= alive:
        issues.append("required_runtime_thread_missing")
    scheduler_age = detail.get("scheduler_age_seconds")
    if not isinstance(scheduler_age, (int, float)) or not math.isfinite(scheduler_age) or scheduler_age > 180:
        issues.append("scheduler_progress_over_180_seconds")
    loops = (detail.get("api_task_worker") or {}).get("loops") or {}
    if any(not (loops.get(name) or {}).get("fresh") for name in ("agent", "brain")):
        issues.append("worker_heartbeat_stale")
    lag = detail.get("event_loop") or {}
    if lag.get("recent_stalls_over_ws_risk") != 0:
        issues.append("event_loop_stall_risk")
    if health.get("status") != "ok" or health.get("issues"):
        issues.extend(f"health:{issue}" for issue in health.get("issues", []) or [health.get("status", "missing")])
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=int, default=1200)
    parser.add_argument("--interval-seconds", type=int, default=15)
    parser.add_argument("--base-url", default="http://127.0.0.1:8003")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.duration_seconds <= 86400 or not 1 <= args.interval_seconds <= 60:
        parser.error("duration must be 1..86400 seconds and interval 1..60 seconds")
    args.output.mkdir(parents=True, exist_ok=False)
    headers = {"x-api-key": os.environ.get("FORVEN_API_KEY", "")}
    samples = []
    deadline = time.monotonic() + args.duration_seconds
    with (args.output / "samples.jsonl").open("w", encoding="utf-8") as stream:
        while time.monotonic() < deadline:
            sample = {"at": datetime.now(timezone.utc).isoformat()}
            started = time.monotonic()
            try:
                request = Request(args.base_url.rstrip("/") + "/api/health", headers=headers)
                with urlopen(request, timeout=15) as response:
                    sample["health"] = json.load(response)
                sample["latency_seconds"] = round(time.monotonic() - started, 3)
                sample["violations"] = assess(sample["health"], sample["latency_seconds"])
            except Exception as exc:
                sample.update(error=f"{type(exc).__name__}: {exc}", violations=["request_failed"])
            samples.append(sample)
            stream.write(json.dumps(sample) + "\n")
            stream.flush()
            report = summarize(samples)
            (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps({"at": sample["at"], "samples": len(samples),
                              "violations": sample["violations"], "latency": sample.get("latency_seconds")}), flush=True)
            time.sleep(max(0, min(args.interval_seconds - (time.monotonic() - started), deadline - time.monotonic())))
    report = summarize(samples)
    report["duration_seconds"] = args.duration_seconds
    report["complete"] = True
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
