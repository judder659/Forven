"""Offline pipeline acceptance trial through production HTTP routes.

Run from the repository: python scripts/pipeline_acceptance.py
Each phase uses a fresh interpreter and an isolated FORVEN_HOME. No API lifespan,
scheduler, agent, exchange or notification service is started. Market files are
copied, never modified in place. Synthetic controls are explicitly labelled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, default=str, allow_nan=False), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def offline() -> None:
    """Fail network attempts at the boundary, without replacing pipeline logic."""
    import socket

    original = socket.socket.connect

    def connect(sock: socket.socket, address: Any) -> Any:
        # Windows implements asyncio's socketpair using an ephemeral loopback
        # connection. Permit it, while explicitly excluding the production API.
        local = isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"} and address[1] != 8003
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not local:
            raise OSError("External network disabled for isolated pipeline acceptance")
        return original(sock, address)

    socket.socket.connect = connect


def client() -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from forven.routers.gauntlet import router as gauntlet
    from forven.routers.lifecycle import router as lifecycle

    app = FastAPI()
    app.include_router(lifecycle)
    app.include_router(gauntlet)
    return TestClient(app)


def call(api: Any, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    response = api.request(method, path, **kwargs)
    response.raise_for_status()
    return response.json()


def initialize(run: Path, source: Path) -> None:
    import numpy as np
    import pandas as pd
    from forven.db import init_db, kv_set
    from forven.data import save_parquet

    init_db()
    # Reduce only the sweep breadth for a bounded trial. Gate thresholds remain
    # the default preset and testing_mode remains False.
    kv_set("forven:pipeline:settings", {
        "gate_sweep_timeframes": ["1h"],
        "gauntlet_auto_quick_screen_enabled": True,
    })
    copied = []
    for symbol in ("BTC-USDT", "ETH-USDT"):
        for filename in ("1h.parquet", "1h.parquet.tail"):
            original = source / symbol / filename
            if not original.is_file():
                continue
            target = run / "home/data/ohlcv" / symbol / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
            copied.append({"source": str(original), "copy": str(target.relative_to(run)),
                           "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})

    # The controls exceed the default 730-day backtest window. Prices are artificial;
    # success on them is software evidence only.
    n = 760 * 24
    timestamps = pd.date_range(end=pd.Timestamp.now(tz="UTC").floor("h") - pd.Timedelta(hours=1), periods=n, freq="h")
    flat = pd.DataFrame({"timestamp": timestamps, "open": 100., "high": 101.,
                         "low": 99., "close": 100., "volume": 10000.})
    save_parquet(flat, "FLAT/USDT", "1h", source="acceptance_synthetic")
    rng = np.random.default_rng(20260907)
    close = 100 + 4 * np.sin(np.arange(n) / 12) + rng.normal(0, 0.8, n)
    opened = np.r_[close[0], close[:-1]]
    oscillating = pd.DataFrame({"timestamp": timestamps, "open": opened,
        "high": np.maximum(opened, close) + .5, "low": np.minimum(opened, close) - .5,
        "close": close, "volume": 10000.})
    save_parquet(oscillating, "CONTROL/USDT", "1h", source="acceptance_synthetic")

    specs = [
        ("BTC stochastic", "stochastic", "BTC/USDT", "market_candidate"),
        ("ETH donchian", "donchian", "ETH/USDT", "market_candidate"),
        ("Oscillating control", "stochastic", "CONTROL/USDT", "synthetic_candidate"),
        ("Zero-trade control", "stochastic", "FLAT/USDT", "must_reject"),
        ("Missing-data control", "stochastic", "ETH/USDT", "must_block_data"),
        ("Data restoration control", "stochastic", "FLAT/USDT", "must_restore_data"),
        ("Restart control", "stochastic", "FLAT/USDT", "must_recover"),
        ("Cancellation control", "stochastic", "FLAT/USDT", "must_cancel"),
        ("Invalid runtime", "acceptance_nonexistent_runtime", "BTC/USDT", "must_reject_intake"),
    ]
    candidates = []
    with client() as api:
        invalid = api.post("/api/lifecycle/strategies", json={
            "name": "Invalid market", "type": "stochastic", "symbol": "NONEXISTENT/USDT",
            "timeframe": "1h", "definition_json": {"params": {}},
        })
        assert invalid.status_code == 422, invalid.text
        candidates.append({"name": "Invalid market", "symbol": "NONEXISTENT/USDT",
                           "type": "stochastic", "expectation": "must_reject_intake",
                           "intake": {"ok": False, "error": invalid.json()["detail"], "http_status": 422}})
        for name, runtime, symbol, expectation in specs:
            response = call(api, "POST", "/api/lifecycle/strategies", json={
                "name": name, "type": runtime, "symbol": symbol,
                "timeframe": "4h" if expectation in {"must_block_data", "must_restore_data"} else "1h",
                "source": "acceptance", "definition_json": {"params": {}},
            })
            if expectation == "must_reject_intake":
                assert response.get("ok") is False and "registered" in response.get("error", ""), response
            else:
                assert response.get("gauntlet_workflow_id"), response
            candidates.append({"name": name, "symbol": symbol, "type": runtime,
                               "expectation": expectation, "intake": response})
    write_json(run / "manifest.json", {"candidates": candidates, "copied_data": copied,
               "synthetic_seed": 20260907, "synthetic_bars": n,
               "synthetic_last_bar": str(timestamps[-1]), "sweep_timeframes": ["1h"]})


def interrupt(run: Path) -> None:
    from datetime import datetime, timedelta, timezone
    from forven.db import get_db
    from forven.gauntlet.engine import claim_next_step

    candidate = next(c for c in read_json(run / "manifest.json")["candidates"] if c["expectation"] == "must_recover")
    claimed = claim_next_step(candidate["intake"]["gauntlet_workflow_id"])
    assert claimed and claimed["attempt_count"] == 1
    # Accelerate elapsed time for the crash drill without weakening recovery's
    # production threshold or sleeping for thirty minutes.
    claimed["started_at"] = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE gauntlet_steps SET started_at=? WHERE id=?", (claimed["started_at"], claimed["id"]))
    write_json(run / "interrupted.json", claimed)
    # Abrupt death after the durable claim, without completion or cleanup.
    os._exit(23)


def recover(run: Path) -> None:
    from forven.db import get_db
    from forven.gauntlet.engine import complete_step, recover_stale_running_steps
    from forven.gauntlet.store import get_workflow_detail

    old = read_json(run / "interrupted.json")
    with client() as api:
        before = call(api, "POST", f"/api/gauntlet/workflows/{old['workflow_id']}/resume")
        assert before["steps_run"] == 0, before
        recovered = recover_stale_running_steps(stale_after_minutes=30)
        assert recovered["blocked_runtime"] == 1, recovered
        call(api, "POST", f"/api/gauntlet/steps/{old['id']}/retry")
        resumed = call(api, "POST", f"/api/gauntlet/workflows/{old['workflow_id']}/resume")
        assert resumed["last_outcome"]["status"] == "passed", resumed
        step = get_workflow_detail(old["workflow_id"])["steps"][0]
        assert step["attempt_count"] == 2 and step["result_id"], step
        complete_step(old["id"], {"status": "passed", "result_id": "forbidden-late-result"},
                      expected_attempt=old["attempt_count"], expected_started_at=old["started_at"])
        after = get_workflow_detail(old["workflow_id"])["steps"][0]
        assert after["result_id"] == step["result_id"], after
        with get_db() as conn:
            strategy_id = get_workflow_detail(old["workflow_id"])["workflow"]["strategy_id"]
            count = conn.execute("SELECT COUNT(*) FROM backtest_results WHERE strategy_id=?", (strategy_id,)).fetchone()[0]
        assert count == 1, count
    write_json(run / "recovery.json", {"ok": True, "old_attempt": 1, "new_attempt": 2,
               "result_id": step["result_id"], "late_result_rejected": True,
               "durable_result_count": count, "resumed": resumed})


def advance(run: Path) -> None:
    from forven.gauntlet.store import get_workflow_detail

    log = []
    with client() as api:
        for candidate in read_json(run / "manifest.json")["candidates"]:
            intake = candidate["intake"]
            wf = intake.get("gauntlet_workflow_id")
            if not wf:
                continue
            if candidate["expectation"] == "must_cancel":
                call(api, "POST", f"/api/gauntlet/workflows/{wf}/cancel")
            before = get_workflow_detail(wf)
            started = time.monotonic()
            outcome = call(api, "POST", f"/api/gauntlet/workflows/{wf}/resume?max_steps=12")
            log.append({"name": candidate["name"], "seconds": round(time.monotonic()-started, 3),
                        "before": before["workflow"]["status"], "outcome": outcome})
            write_json(run / "progress.json", log)
            latest = outcome.get("last_outcome") or {}
            print(candidate["name"], latest.get("status"), latest.get("message"), flush=True)
        # Poll genuine async jobs to an explicit decision/block. A submitted
        # optimization is progress, never a completed acceptance result.
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            active = []
            for candidate in read_json(run / "manifest.json")["candidates"]:
                wf = candidate["intake"].get("gauntlet_workflow_id")
                if wf and get_workflow_detail(wf)["workflow"]["status"] in {"pending", "queued", "running"}:
                    active.append(candidate)
            if not active:
                break
            for candidate in active:
                wf = candidate["intake"]["gauntlet_workflow_id"]
                started = time.monotonic()
                outcome = call(api, "POST", f"/api/gauntlet/workflows/{wf}/resume?max_steps=12")
                latest = outcome.get("last_outcome") or {}
                log.append({"name": candidate["name"], "seconds": round(time.monotonic()-started, 3), "outcome": outcome})
                write_json(run / "progress.json", log)
                print(candidate["name"], latest.get("status"), latest.get("message"), flush=True)
            time.sleep(2)


def restore_data(run: Path) -> None:
    import pandas as pd
    from forven.data import load_parquet, save_parquet
    from forven.gauntlet.store import get_workflow_detail

    candidate = next(c for c in read_json(run / "manifest.json")["candidates"] if c["expectation"] == "must_restore_data")
    wf = candidate["intake"]["gauntlet_workflow_id"]
    before = get_workflow_detail(wf)
    assert before["workflow"]["status"] == "blocked_data", before["workflow"]
    candles = load_parquet("FLAT/USDT", "1h")
    restored = candles.set_index("timestamp").resample("4h", label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
    }).dropna().reset_index()
    # Only full 4-hour candles: neither partial leading nor trailing bins.
    restored = restored[(restored.timestamp >= candles.timestamp.iloc[0]) &
                        (restored.timestamp + pd.Timedelta(hours=3) <= candles.timestamp.iloc[-1])]
    save_parquet(restored, "FLAT/USDT", "4h", source="acceptance_synthetic")
    with client() as api:
        call(api, "POST", f"/api/gauntlet/steps/{before['steps'][0]['id']}/retry")
        after = call(api, "POST", f"/api/gauntlet/workflows/{wf}/resume?max_steps=12")
    detail = get_workflow_detail(wf)
    assert detail["steps"][0]["status"] == "passed", after
    assert detail["steps"][0]["attempt_count"] == 2, detail["steps"][0]
    assert detail["workflow"]["status"] == "failed_gate", after  # now scoreable, correctly rejected
    write_json(run / "data-restoration.json", {"ok": True, "before": before, "after": detail,
               "restored_bars": len(restored), "result_id": detail["steps"][0]["result_id"]})


def report(run: Path) -> None:
    from forven.db import get_db
    from forven.gauntlet.store import get_workflow_detail, sanitize_non_finite
    from forven.policy import load_pipeline_config
    from forven.engine_provenance import BACKTEST_ENGINE_VERSION

    rows = []
    checks = []
    with client() as api:
        for candidate in read_json(run / "manifest.json")["candidates"]:
            intake = candidate["intake"]
            wf = intake.get("gauntlet_workflow_id")
            if not wf:
                rows.append({**candidate, "outcome": "intake_rejected", "reason": intake.get("error")})
                checks.append({"name": candidate["name"], "ok": intake.get("ok") is False})
                continue
            detail = get_workflow_detail(wf)
            status = detail["workflow"]["status"]
            unresolved = next((s for s in detail["steps"] if s["status"] != "passed"), None)
            explanation = call(api, "GET", f"/api/lifecycle/strategies/{intake['id']}/explain")
            with get_db() as conn:
                results = [dict(r) for r in conn.execute(
                    "SELECT result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json FROM backtest_results WHERE strategy_id=?",
                    (intake["id"],)).fetchall()]
                stage = conn.execute("SELECT stage FROM strategies WHERE id=?", (intake["id"],)).fetchone()[0]
            for result in results:
                result["metrics"] = json.loads(result.pop("metrics_json") or "{}")
                result["config"] = json.loads(result.pop("config_json") or "{}")
            expectation = candidate["expectation"]
            expected = {"must_reject": "failed_gate", "must_block_data": "blocked_data",
                        "must_recover": "failed_gate", "must_restore_data": "failed_gate",
                        "must_cancel": "cancelled"}.get(expectation)
            checks.append({"name": candidate["name"], "ok": status == expected if expected else status in {
                "passed", "failed_gate", "blocked_data", "blocked_runtime", "blocked_operator"},
                "expected": expected or "explicit decision or actionable block", "actual": status})
            if status != "passed":
                checks.append({"name": candidate["name"] + " has not entered paper", "ok": stage != "paper"})
            if status == "blocked_data":
                block = explanation["strategy"]["blockers"][0]
                action = "review_strategy" if block.get("code") == "insufficient_evidence" else "fix_data"
                checks.append({"name": candidate["name"] + " explains data repair",
                               "ok": explanation["strategy"]["next_action"]["key"] == action})
            reason_json = (unresolved or {}).get("error_json") or (unresolved or {}).get("output_json") or "{}"
            reason_payload = json.loads(reason_json)
            reason = reason_payload.get("message") or status
            rows.append({**candidate, "outcome": status, "stage": stage,
                         "current_step": (unresolved or {}).get("step_key"), "reason": reason,
                         "workflow": detail, "explanation": explanation, "results": results})
        # Confirm that reopening a cancelled workflow cannot resurrect it.
        cancelled = next(r for r in rows if r["expectation"] == "must_cancel")
        wf = cancelled["intake"]["gauntlet_workflow_id"]
        call(api, "POST", f"/api/gauntlet/workflows/{wf}/resume")
        checks.append({"name": "Cancellation survives a fresh process", "ok": get_workflow_detail(wf)["workflow"]["status"] == "cancelled"})
    payload = sanitize_non_finite({"engine_version": BACKTEST_ENGINE_VERSION,
              "pipeline_config": load_pipeline_config(), "checks": checks, "candidates": rows,
              "recovery": read_json(run / "recovery.json"),
              "data_restoration": read_json(run / "data-restoration.json"),
              "ok": all(c["ok"] for c in checks)})
    write_json(run / "report.json", payload)
    lines = ["# Controlled pipeline acceptance trial", "", f"Engine version: {BACKTEST_ENGINE_VERSION}", "",
             f"Acceptance assertions: {'PASS' if payload['ok'] else 'FAIL'} ({sum(c['ok'] for c in checks)}/{len(checks)})", "",
             "| Candidate | Outcome | Stopping step | Results |", "|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row['name']} | {row['outcome']} | {row.get('current_step') or '-'} | {len(row.get('results', []))} |")
    lines += ["", "## Evidence and scope", "",
        "Production lifecycle and gauntlet HTTP handlers ran in an isolated database with real backtests, data checks, and gate decisions. No gate verdicts were substituted. The HTTP transport used FastAPI TestClient; the app lifespan, scheduler, agents and trading services were not started.", "",
        "Each phase ran in a fresh Python process. A control process exited abruptly after claiming a step. The claim timestamp was aged by 31 minutes to exercise the normal 30-minute recovery threshold without a long sleep. The replacement process refused duplicate dispatch, recovered and retried it as attempt 2, persisted one result, and rejected a late attempt-1 result. This tests pipeline-worker persistence, not a full desktop/backend restart.", "",
        "A separate control was first blocked for missing 4h data, then given complete 4h bars aggregated from its synthetic 1h dataset. Retrying produced a real backtest and the correct quality rejection. No quality threshold was relaxed.", "",
        "Market datasets were copied with SHA-256 hashes recorded in manifest.json. FLAT and CONTROL datasets are synthetic and labelled acceptance_synthetic. Their results are not investment evidence. The one-timeframe sweep bounds trial cost; default gate thresholds remain enabled.", "",
        "A data/runtime block is an explicit outcome, not successful strategy validation. A clean acceptance result does not mean all twelve stages ran for every candidate. Per-stage outcomes, explanations, settings, parameters and artifacts are in report.json. No production strategy was changed.", "", "## Candidate reasons", ""]
    for row in rows:
        lines += [f"### {row['name']}", "", str(row.get("reason")), ""]
    (run / "report.md").write_text("\n".join(lines), encoding="utf-8")
    assert payload["ok"], "Acceptance assertion failed; see report.json"


def background_interrupt(run: Path) -> None:
    """Crash after a durable background claim, before the controlled job finishes."""
    import threading

    from forven.gauntlet.engine import claim_next_step
    from forven.gauntlet.store import get_workflow_detail
    from forven.gauntlet.worker import run_or_poll

    with client() as api:
        intake = call(api, "POST", "/api/lifecycle/strategies", json={
            "name": "Background restart drill", "type": "stochastic", "symbol": "FLAT/USDT",
            "timeframe": "1h", "source": "acceptance", "definition_json": {"params": {}},
        })
    wf_id = intake["gauntlet_workflow_id"]
    step = claim_next_step(wf_id)
    entered = threading.Event()
    release = threading.Event()

    def controlled_pending_job(*args: Any) -> dict:
        entered.set()
        release.wait(60)
        return {"status": "blocked_runtime", "retryable": True, "message": "Synthetic interruption control"}

    outcome = run_or_poll(get_workflow_detail(wf_id)["workflow"], step, controlled_pending_job)
    assert outcome.get("background_job_id") and entered.wait(5), outcome
    write_json(run / "background-interrupted.json", {"step": step, "outcome": outcome})
    os._exit(23)


def background_recover(run: Path) -> None:
    from forven.gauntlet.engine import complete_step
    from forven.gauntlet.store import get_workflow_detail

    old = read_json(run / "background-interrupted.json")["step"]
    with client() as api:
        polled = call(api, "POST", f"/api/gauntlet/workflows/{old['workflow_id']}/resume")
        assert polled["last_outcome"]["status"] == "blocked_runtime", polled
        call(api, "POST", f"/api/gauntlet/steps/{old['id']}/retry")
        resumed = call(api, "POST", f"/api/gauntlet/workflows/{old['workflow_id']}/resume")
        # The retry resumes in a real bounded background worker because the
        # previous job handle remains on the step, with a different attempt.
        deadline = time.monotonic() + 600
        while resumed["last_outcome"].get("status") == "running" and time.monotonic() < deadline:
            time.sleep(0.5)
            resumed = call(api, "POST", f"/api/gauntlet/workflows/{old['workflow_id']}/resume")
        assert resumed["last_outcome"]["status"] == "passed", resumed
        step = get_workflow_detail(old["workflow_id"])["steps"][0]
        assert step["attempt_count"] == 2 and step["result_id"], step
        complete_step(old["id"], {"status": "passed", "result_id": "forbidden-background-late-result"},
                      expected_attempt=old["attempt_count"], expected_started_at=old["started_at"])
        assert get_workflow_detail(old["workflow_id"])["steps"][0]["result_id"] == step["result_id"]
    write_json(run / "background-recovery.json", {
        "ok": True, "old_attempt": 1, "new_attempt": 2, "result_id": step["result_id"],
        "late_result_rejected": True,
        "scope": "Abrupt process death during a synthetic pending job; fresh process retries a real backtest on labelled synthetic FLAT candles",
    })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-data", type=Path, default=Path.home() / ".forven/data/ohlcv")
    parser.add_argument("--phase", choices=["initialize", "interrupt", "recover", "advance", "restore_data", "background_interrupt", "background_recover", "report"])
    args = parser.parse_args()
    if args.phase:
        run = args.output.resolve()
        assert Path(os.environ["FORVEN_HOME"]).resolve() == run / "home"
        import forven.config as cfg

        cfg.LEGACY_WORKSPACE_DIR = run / "legacy-workspace"
        cfg.OPENCLAW_WORKSPACE = run / "openclaw-workspace"
        offline()
        if args.phase == "initialize":
            initialize(run, args.source_data.resolve())
        else:
            {"interrupt": interrupt, "recover": recover, "advance": advance,
             "restore_data": restore_data, "background_interrupt": background_interrupt,
             "background_recover": background_recover, "report": report}[args.phase](run)
        return

    run = (args.output or ROOT / ".tmp" / f"pipeline-acceptance-{time.strftime('%Y%m%d-%H%M%S')}").resolve()
    run.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ)
    env.update(FORVEN_HOME=str(run / "home"), FORVEN_DATA_DIR=str(run / "home/data/ohlcv"),
               FORVEN_ALLOW_MAINNET="0", FORVEN_API_CONTROL_PLANE_ONLY="1", FORVEN_API_KEY="",
               FORVEN_OPERATOR_KEY="", FORVEN_AUTH_REQUIRED="0", PYTHONUTF8="1")
    try:
        with urlopen("http://127.0.0.1:8003/api/health", timeout=5) as response:
            write_json(run / "production-health-before.json", json.load(response))
    except Exception as exc:
        write_json(run / "production-health-before.json", {"error": str(exc)})
    print(f"Trial directory: {run}", flush=True)
    phases = []
    for phase in ("initialize", "interrupt", "recover", "advance", "restore_data", "background_interrupt", "background_recover", "report"):
        started = time.monotonic()
        with (run / f"{phase}.log").open("w", encoding="utf-8") as log:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--phase", phase,
                       "--output", str(run), "--source-data", str(args.source_data.resolve())],
                       cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=900)
        expected = 23 if phase in {"interrupt", "background_interrupt"} else 0
        seconds = round(time.monotonic()-started, 3)
        phases.append({"phase": phase, "exit_code": result.returncode, "seconds": seconds,
                       "ok": result.returncode == expected})
        write_json(run / "phases.json", phases)
        print(f"{phase}: exit {result.returncode}, {seconds:.1f}s", flush=True)
        if result.returncode != expected:
            raise SystemExit(f"Trial failed in {phase}; inspect {run / (phase + '.log')}")
    try:
        with urlopen("http://127.0.0.1:8003/api/health", timeout=5) as response:
            write_json(run / "production-health-after.json", json.load(response))
    except Exception as exc:
        write_json(run / "production-health-after.json", {"error": str(exc)})
    print(f"Report: {run / 'report.md'}", flush=True)


if __name__ == "__main__":
    main()
