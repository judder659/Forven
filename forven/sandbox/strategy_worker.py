"""Out-of-process execution of UNTRUSTED strategy signal generation (Phase 2).

The execution kernel (:mod:`forven.strategies.execution_kernel`) is first-party and
trusted; the only untrusted code on the path is a strategy's signal logic
(``generate_signals`` / ``generate_signal``). Running it in-process means a strategy
that slips past the AST guard gets the host's full environment (the live HyperLiquid
key, the Fernet key) and unrestricted FS/network. This module moves that step into a
subprocess that:

  * inherits a **secret-free** environment (``build_subprocess_env``),
  * has **network egress denied** (the strategy can compute, not exfiltrate),
  * is confined to a throwaway working directory,
  * is memory/CPU/process-capped (Win32 Job Object / POSIX rlimit, from :mod:`forven.sandbox`).

The strategy module is imported INSIDE the worker (under the AST guard), so a custom/
untrusted strategy never executes in the trusted parent. Transport is parquet, never
pickle: the parent writes the OHLCV frame, the worker writes four boolean signal
columns back, and the parent reads them as *data* and re-validates the schema — a
compromised worker therefore cannot achieve code execution in the trusted parent.

PERFORMANCE: a worker imports forven and runs ``registry.discover()`` ONCE at startup
(~seconds), then SERVES many signal-gen requests over a pipe. A module-level persistent
worker (per process) is lazily spawned, reused across calls, and respawned on death or
timeout, so the discover() cost is amortized rather than paid per call.

NOTE (Phase 2 status): this is the isolation primitive + a persistent worker. Wiring it
into the backtest/scanner hot paths — so the parent stops importing custom strategy code
and delegates per-bar execution too — is the remaining increment (see
docs/security-hardening-plan.md). A strategy run here sees ONLY the input frame (no DB,
no network), so the trusted parent must enrich the df with any funding/OI/cross-asset
columns before delegating.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pandas as pd

from forven.sandbox import (
    IS_WINDOWS,
    PYTHON_EXE,
    REPO_ROOT,
    _BLAS_THREAD_ENV,
    _assign_pid_to_job,
    _build_posix_preexec,
    _close_job,
    _create_windows_job_object,
)
from forven.security.env_allowlist import build_subprocess_env
from forven.strategies.base import DirectionalSignals

# A worker spawns with this var set; the flag check in backtest treats it as
# "already isolated" so the worker runs signal-gen IN-PROCESS rather than
# recursively spawning another worker.
WORKER_ENV_FLAG = "FORVEN_IN_STRATEGY_WORKER"

_SIGNAL_COLUMNS = ("long_entries", "long_exits", "short_entries", "short_exits")

DEFAULT_TIMEOUT_SECONDS = 120  # per-request, on an already-warm worker
READY_TIMEOUT_SECONDS = 90  # startup: import forven + registry.discover()
PERSISTENT_MAX_MEMORY_MB = 2048  # worker-lifetime cap (set once at spawn)


class StrategyWorkerError(RuntimeError):
    """Raised when the isolated worker fails to produce valid signals."""


# ---------------------------------------------------------------------------
# Worker side (runs in the locked-down subprocess)
# ---------------------------------------------------------------------------

def _install_network_deny() -> None:
    """Best-effort: make outbound (routable) sockets raise inside the worker.

    Defense-in-depth — the env is already secret-free and the AST guard blocks
    ``import socket`` in strategy code; this closes the residual where a guard
    bypass reaches a socket via a transitive import. Loopback is left alone."""
    try:
        import socket
    except Exception:
        return

    def _deny(*_a, **_k):  # noqa: ANN002, ANN003
        raise OSError("network access is disabled in the strategy sandbox")

    _real_socket = socket.socket

    class _GuardedSocket(_real_socket):  # type: ignore[misc, valid-type]
        def connect(self, address):  # noqa: ANN001
            self._refuse_if_routable(address)
            return super().connect(address)

        def connect_ex(self, address):  # noqa: ANN001
            self._refuse_if_routable(address)
            return super().connect_ex(address)

        @staticmethod
        def _refuse_if_routable(address) -> None:  # noqa: ANN001
            host = address[0] if isinstance(address, tuple) and address else ""
            if str(host).strip().lower() in ("127.0.0.1", "::1", "localhost", ""):
                return
            raise OSError("network access is disabled in the strategy sandbox")

    socket.socket = _GuardedSocket  # type: ignore[misc, assignment]
    socket.create_connection = _deny  # type: ignore[assignment]


def _compute_signals(workdir: Path) -> None:
    """Build the requested strategy and write its DirectionalSignals (4 bool columns)
    to out.parquet. Assumes the registry is already populated (discover() done).
    Runs the UNTRUSTED strategy's generate_signals."""
    from forven.strategies import registry
    from forven.strategies.backtest import _normalize_directional_signal_payload

    request = json.loads((workdir / "request.json").read_text(encoding="utf-8"))
    df = pd.read_parquet(workdir / "in.parquet")

    strategy_type = str(request["strategy_type"])
    cls = registry._TYPE_MAP.get(strategy_type)
    if cls is None:
        raise StrategyWorkerError(f"unknown strategy type {strategy_type!r}")
    strat = cls("isolated", dict(request.get("params") or {}))
    payload = strat.generate_signals(df)
    signals = _normalize_directional_signal_payload(
        payload,
        df.index,
        trade_mode=str(request.get("trade_mode") or "long_only"),
        default_direction=str(request.get("default_direction") or "long"),
    )
    out = pd.DataFrame(
        {
            "long_entries": signals.long_entries.astype(bool).to_numpy(),
            "long_exits": signals.long_exits.astype(bool).to_numpy(),
            "short_entries": signals.short_entries.astype(bool).to_numpy(),
            "short_exits": signals.short_exits.astype(bool).to_numpy(),
        },
        index=df.index,
    )
    out.to_parquet(workdir / "out.parquet")


def _prepare_worker_runtime():
    """Import the trusted forven modules, deny network, then discover() the registry
    (which imports strategy modules — custom top-level code runs HERE, under the
    AST guard, network-denied)."""
    import forven.strategies.backtest  # noqa: F401 — warm the (trusted) import
    from forven.strategies import registry

    _install_network_deny()
    try:
        registry.discover()
    except Exception:
        # discover() skips individual broken modules itself; a total failure here
        # still leaves builtins registered, and unknown types fail per-request.
        pass


def _run_worker(workdir: Path) -> int:
    """One-shot entry point: prepare runtime, compute one request, exit."""
    status_path = workdir / "status.json"
    try:
        _prepare_worker_runtime()
        _compute_signals(workdir)
        status_path.write_text(json.dumps({"ok": True}), encoding="utf-8")
        return 0
    except BaseException as exc:  # noqa: BLE001 — report ANY failure as structured status
        try:
            status_path.write_text(
                json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:2000]}),
                encoding="utf-8",
            )
        except Exception:
            pass
        return 1


def _serve() -> int:
    """Persistent entry point: prepare runtime ONCE, then loop on newline-delimited
    JSON requests from stdin (each names a workdir), acking each on stdout."""
    _prepare_worker_runtime()
    sys.stdout.write(json.dumps({"ready": True}) + "\n")
    sys.stdout.flush()
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        if line == "__shutdown__":
            break
        try:
            msg = json.loads(line)
            workdir = Path(msg["workdir"])
            _compute_signals(workdir)
            (workdir / "status.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
            ack = {"ok": True}
        except BaseException as exc:  # noqa: BLE001 — a bad request must not kill the worker
            ack = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:2000]}
        sys.stdout.write(json.dumps(ack) + "\n")
        sys.stdout.flush()
    return 0


# ---------------------------------------------------------------------------
# Parent side (trusted host process): a persistent, reusable worker
# ---------------------------------------------------------------------------

def _build_worker_env() -> dict:
    existing_pythonpath = str(os.environ.get("PYTHONPATH") or "").strip()
    repo_root = str(REPO_ROOT)
    pythonpath = repo_root if not existing_pythonpath else f"{repo_root}{os.pathsep}{existing_pythonpath}"
    extra = {"PYTHONPATH": pythonpath, WORKER_ENV_FLAG: "1"}
    for _k, _v in _BLAS_THREAD_ENV.items():
        extra[_k] = os.environ.get(_k, _v)
    env = build_subprocess_env(extra=extra)
    env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/usr/local/bin"))
    env.setdefault("HOME", tempfile.gettempdir())
    return env


class _PersistentWorker:
    """A long-lived signal-gen subprocess. Imports + discover() once, then serves
    many requests. A daemon thread drains stdout into a queue so requests can time
    out portably (no select on Windows pipes)."""

    def __init__(self) -> None:
        self._acks: "queue.Queue[dict]" = queue.Queue()
        self._proc: subprocess.Popen | None = None
        self._job = None
        self._kernel32 = None
        self._stderr_f = None
        self._stderr_path = Path(tempfile.gettempdir()) / f"forven_strat_worker_{os.getpid()}.stderr.log"
        self._spawn()

    def _spawn(self) -> None:
        env = _build_worker_env()
        self._stderr_f = open(self._stderr_path, "w", encoding="utf-8")  # a FILE (won't deadlock on a full pipe)
        cmd = [PYTHON_EXE, "-m", "forven.sandbox.strategy_worker", "--serve"]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_f,
            text=True,
            bufsize=1,
            env=env,
            cwd=str(REPO_ROOT),
            preexec_fn=(None if IS_WINDOWS else _build_posix_preexec(PERSISTENT_MAX_MEMORY_MB)),
        )
        if IS_WINDOWS:
            self._job, self._kernel32 = _create_windows_job_object(PERSISTENT_MAX_MEMORY_MB)
            if self._job and self._kernel32:
                _assign_pid_to_job(self._job, self._kernel32, self._proc.pid)
        threading.Thread(target=self._read_loop, daemon=True).start()
        try:
            ready = self._acks.get(timeout=READY_TIMEOUT_SECONDS)
        except queue.Empty:
            self.shutdown()
            raise StrategyWorkerError(f"strategy worker did not become ready: {self.stderr_tail()}")
        if not ready.get("ready"):
            self.shutdown()
            raise StrategyWorkerError("strategy worker failed to report ready")

    def _read_loop(self) -> None:
        try:
            assert self._proc is not None and self._proc.stdout is not None
            for raw in self._proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    self._acks.put(json.loads(line))
                except Exception:
                    pass
        finally:
            self._acks.put({"_eof": True})

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def request(self, workdir: Path, timeout: int) -> dict:
        if not self.alive() or self._proc is None or self._proc.stdin is None:
            raise StrategyWorkerError("strategy worker is not alive")
        self._proc.stdin.write(json.dumps({"workdir": str(workdir)}) + "\n")
        self._proc.stdin.flush()
        ack = self._acks.get(timeout=timeout)  # raises queue.Empty on timeout
        if ack.get("_eof"):
            raise StrategyWorkerError("strategy worker exited mid-request")
        return ack

    def stderr_tail(self) -> str:
        try:
            if self._stderr_f is not None:
                self._stderr_f.flush()
            return self._stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:].strip()
        except Exception:
            return ""

    def shutdown(self) -> None:
        try:
            if self.alive() and self._proc is not None and self._proc.stdin is not None:
                self._proc.stdin.write("__shutdown__\n")
                self._proc.stdin.flush()
                self._proc.wait(timeout=3)
        except Exception:
            pass
        try:
            if self.alive() and self._proc is not None:
                self._proc.kill()
        except Exception:
            pass
        _close_job(self._job, self._kernel32)
        try:
            if self._stderr_f is not None:
                self._stderr_f.close()
        except Exception:
            pass


_worker_lock = threading.Lock()
_worker: "_PersistentWorker | None" = None


def _reset_worker() -> None:
    global _worker
    if _worker is not None:
        try:
            _worker.shutdown()
        except Exception:
            pass
    _worker = None


def _get_worker() -> "_PersistentWorker":
    global _worker
    if _worker is not None and _worker.alive():
        return _worker
    _reset_worker()
    _worker = _PersistentWorker()
    return _worker


atexit.register(_reset_worker)


def compute_directional_signals_isolated(
    df: pd.DataFrame,
    strategy_type: str,
    params: dict,
    *,
    trade_mode: str,
    default_direction: str = "long",
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> DirectionalSignals:
    """Build the strategy and run its ``generate_signals(df)`` in an isolated,
    secret-free, network-denied subprocess, returning normalized DirectionalSignals.

    Uses a persistent per-process worker (spawned lazily, reused across calls). Output
    is identical to building the same strategy in-process and normalizing its
    ``generate_signals`` payload — only the trust boundary differs. Raises
    :class:`StrategyWorkerError` on timeout, worker error, or malformed output; the
    caller does NOT silently fall back to in-process execution (fail closed).
    """
    with tempfile.TemporaryDirectory(prefix="forven_strat_") as tmp:
        workdir = Path(tmp)
        try:
            df.to_parquet(workdir / "in.parquet")
        except Exception as exc:  # a non-serializable frame is a programming error
            raise StrategyWorkerError(f"failed to serialize input frame: {exc}") from exc
        (workdir / "request.json").write_text(
            json.dumps(
                {
                    "strategy_type": strategy_type,
                    "params": params or {},
                    "trade_mode": trade_mode,
                    "default_direction": default_direction,
                }
            ),
            encoding="utf-8",
        )

        with _worker_lock:
            try:
                worker = _get_worker()
                ack = worker.request(workdir, timeout)
            except queue.Empty:
                # A late ack would corrupt the next request's exchange → respawn.
                _reset_worker()
                raise StrategyWorkerError(
                    f"isolated signal generation for {strategy_type!r} timed out after {timeout}s"
                )
            except StrategyWorkerError:
                tail = _worker.stderr_tail() if _worker is not None else ""
                _reset_worker()
                raise StrategyWorkerError(
                    f"isolated worker for {strategy_type!r} died"
                    + (f": {tail}" if tail else "")
                )

        if not ack.get("ok"):
            detail = ack.get("error") or _read_status_error(workdir) or "unknown error"
            raise StrategyWorkerError(f"isolated signal generation for {strategy_type!r} failed: {detail}")

        out_path = workdir / "out.parquet"
        if not out_path.exists():
            raise StrategyWorkerError(f"isolated worker for {strategy_type!r} produced no output")
        return _read_and_validate_signals(out_path, df.index, strategy_type)


def _read_status_error(workdir: Path) -> str:
    try:
        status = json.loads((workdir / "status.json").read_text(encoding="utf-8"))
        return str(status.get("error") or "")
    except Exception:
        return ""


def _read_and_validate_signals(out_path: Path, index: pd.Index, strategy_type: str) -> DirectionalSignals:
    """Read the worker's parquet output as DATA and re-validate its schema before
    trusting it. Parquet carries no executable payload, and we never accept a column
    set / length / index we did not ask for."""
    out = pd.read_parquet(out_path)
    missing = [c for c in _SIGNAL_COLUMNS if c not in out.columns]
    if missing:
        raise StrategyWorkerError(
            f"isolated worker for {strategy_type!r} returned columns {list(out.columns)} (missing {missing})"
        )
    if len(out) != len(index):
        raise StrategyWorkerError(
            f"isolated worker for {strategy_type!r} returned {len(out)} rows, expected {len(index)}"
        )
    if not out.index.equals(index):
        raise StrategyWorkerError(f"isolated worker for {strategy_type!r} returned a misaligned index")
    return DirectionalSignals(
        long_entries=out["long_entries"].astype(bool),
        long_exits=out["long_exits"].astype(bool),
        short_entries=out["short_entries"].astype(bool),
        short_exits=out["short_exits"].astype(bool),
    )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        raise SystemExit(_serve())
    _wd = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    raise SystemExit(_run_worker(_wd))
