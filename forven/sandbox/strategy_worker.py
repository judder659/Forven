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
VALIDATE_TIMEOUT_SECONDS = 60  # one-shot import+probe+certify+lookahead of one module


def _coerce_json(o):  # noqa: ANN001, ANN201
    for attr in ("item", "tolist"):
        fn = getattr(o, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    return str(o)


def _json_safe(value):  # noqa: ANN001, ANN201
    """Coerce probe metadata (which may hold numpy scalars / other non-JSON types)
    into a plain JSON-serializable structure for transport back to the parent as
    DATA. Never returns a live Python object across the trust boundary."""
    try:
        return json.loads(json.dumps(value, default=_coerce_json))
    except Exception:
        return {}


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


def _compute_signals(workdir: Path) -> bool:
    """Build the requested strategy and write its DirectionalSignals (4 bool columns)
    to out.parquet. Assumes the registry is already populated (discover() done).
    Runs the UNTRUSTED strategy's generate_signals. Returns True when vectorized
    signals were produced, False when the strategy has NO vectorized generate_signals
    (payload None) — the parent then falls back to the per-bar path, exactly as the
    in-process ``_resolve_strategy_vectorized_signals`` returning None does."""
    from forven.strategies import registry
    from forven.strategies.backtest import (
        _normalize_directional_signal_payload,
        _signals_from_per_bar,
    )

    request = json.loads((workdir / "request.json").read_text(encoding="utf-8"))
    df = pd.read_parquet(workdir / "in.parquet")

    strategy_type = str(request["strategy_type"])
    cls = registry._TYPE_MAP.get(strategy_type)
    if cls is None:
        raise StrategyWorkerError(f"unknown strategy type {strategy_type!r}")
    strat = cls("isolated", dict(request.get("params") or {}))
    trade_mode = str(request.get("trade_mode") or "long_only")

    if str(request.get("mode") or "vectorized") == "per_bar":
        # Walk the strategy's per-bar generate_signal in isolation, with the SAME
        # purity guard + bounded trailing window the in-process adapter uses.
        signals = _signals_from_per_bar(
            strat, df, warmup=int(request.get("warmup") or 0), trade_mode=trade_mode
        )
        if signals is None:
            return False
    else:
        payload = strat.generate_signals(df)
        if payload is None:
            return False
        signals = _normalize_directional_signal_payload(
            payload,
            df.index,
            trade_mode=trade_mode,
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
    return True


def _validate_custom_module(workdir: Path) -> dict:
    """Import + probe + certify + lookahead-scan one CUSTOM strategy module entirely
    inside the locked-down child, returning ONLY JSON metadata.

    This is the lifecycle the audit (docs/strategy-share-security-audit-2026-06-29.md)
    found unguarded: ``register_custom_strategy_file`` runs the untrusted module's
    top-level code (importlib.import_module), its ``__init__`` (probe construction)
    and ``generate_signals`` (lookahead probe) IN THE TRUSTED PARENT. Here the same
    steps run in a subprocess with a secret-free env, network denied, FS confined,
    and resource-capped — so a guard bypass cannot reach host credentials. Only the
    resulting metadata (type/params/asset/certified/lookahead) crosses back, as data."""
    request = json.loads((workdir / "request.json").read_text(encoding="utf-8"))
    module_name = str(request["module_name"])

    import importlib

    from forven.strategies import registry
    from forven.strategies.certification import certify_execution_strategy
    from forven.strategies.lookahead_probe import detect_lookahead

    # Belt-and-suspenders re-scan in the child (the parent already scanned before
    # write; re-checking here means the worker never imports an unscanned module).
    registry.assert_custom_module_safe(module_name)

    module = importlib.import_module(f"forven.strategies.custom.{module_name}")

    strategy_cls = getattr(module, "STRATEGY_CLASS", None)
    if isinstance(strategy_cls, str):
        strategy_cls = getattr(module, strategy_cls, None)
    if not isinstance(strategy_cls, type):
        subclasses = [
            obj
            for obj in vars(module).values()
            if isinstance(obj, type)
            and issubclass(obj, registry.BaseStrategy)
            and obj is not registry.BaseStrategy
            and getattr(obj, "__module__", None) == module.__name__
        ]
        if len(subclasses) == 1:
            strategy_cls = subclasses[0]

    type_name = getattr(module, "TYPE_NAME", None)
    if not type_name and strategy_cls is not None:
        type_name = getattr(strategy_cls, "TYPE_NAME", None)

    if not strategy_cls:
        return {"ok": False, "error": "missing STRATEGY_CLASS"}
    if not type_name:
        return {"ok": False, "error": "missing TYPE_NAME"}

    validation_errors = registry._registry_type_validation_errors(strategy_cls)
    if validation_errors:
        return {"ok": False, "error": "class validation: " + "; ".join(validation_errors)}

    probe = strategy_cls("__probe__", {})
    default_params = probe.default_params
    asset = probe.asset if hasattr(probe, "asset") else "BTC"

    cert = certify_execution_strategy(str(type_name), default_params)
    lookahead_reason = detect_lookahead(probe)

    return {
        "ok": True,
        "type_name": str(type_name),
        "default_params": _json_safe(default_params),
        "canonical_params": _json_safe(getattr(cert, "canonical_params", None)),
        "asset": str(asset).strip() or "BTC",
        "certified": bool(cert.certified),
        "cert_error": cert.primary_blocking_reason(),
        "lookahead_blocked": bool(lookahead_reason),
        "lookahead_reason": lookahead_reason,
    }


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
        produced = _compute_signals(workdir)
        status_path.write_text(json.dumps({"ok": True, "produced": produced}), encoding="utf-8")
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


def _run_validate(workdir: Path) -> int:
    """One-shot entry point: network-deny, validate one custom module, write
    result.json, exit. Does NOT discover() the whole registry — it imports only the
    single target module (its top-level code runs here, contained)."""
    try:
        _install_network_deny()
        result = _validate_custom_module(workdir)
    except BaseException as exc:  # noqa: BLE001 — report ANY failure as structured data
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:2000]}
    try:
        (workdir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    except Exception:
        pass
    return 0 if result.get("ok") else 1


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
            produced = _compute_signals(workdir)
            (workdir / "status.json").write_text(json.dumps({"ok": True, "produced": produced}), encoding="utf-8")
            ack = {"ok": True, "produced": produced}
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


def _request_signals(
    df: pd.DataFrame, request: dict, strategy_type: str, timeout: int
) -> "DirectionalSignals | None":
    """Send one signal-gen request to the persistent worker and return validated,
    schema-checked DirectionalSignals — or ``None`` when the worker produced none for
    this mode (the strategy has no signals there → caller falls back). Raises
    :class:`StrategyWorkerError` on timeout / worker death / malformed output (fail
    closed — never a silent in-process fallback, which would defeat the isolation)."""
    with tempfile.TemporaryDirectory(prefix="forven_strat_") as tmp:
        workdir = Path(tmp)
        try:
            df.to_parquet(workdir / "in.parquet")
        except Exception as exc:  # a non-serializable frame is a programming error
            raise StrategyWorkerError(f"failed to serialize input frame: {exc}") from exc
        (workdir / "request.json").write_text(json.dumps(request), encoding="utf-8")

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
                    f"isolated worker for {strategy_type!r} died" + (f": {tail}" if tail else "")
                )

        if not ack.get("ok"):
            detail = ack.get("error") or _read_status_error(workdir) or "unknown error"
            raise StrategyWorkerError(f"isolated signal generation for {strategy_type!r} failed: {detail}")
        if not ack.get("produced", True):
            return None  # strategy produced no signals for this mode → caller falls back

        out_path = workdir / "out.parquet"
        if not out_path.exists():
            raise StrategyWorkerError(f"isolated worker for {strategy_type!r} produced no output")
        return _read_and_validate_signals(out_path, df.index, strategy_type)


def compute_directional_signals_isolated(
    df: pd.DataFrame,
    strategy_type: str,
    params: dict,
    *,
    trade_mode: str,
    default_direction: str = "long",
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> "DirectionalSignals | None":
    """Run the strategy's VECTORIZED ``generate_signals(df)`` in the isolated worker.
    Returns normalized DirectionalSignals, or ``None`` when the strategy has no
    vectorized ``generate_signals`` (caller falls back to the per-bar path). Output is
    byte-identical to building the strategy in-process and normalizing its payload."""
    return _request_signals(
        df,
        {
            "mode": "vectorized",
            "strategy_type": strategy_type,
            "params": params or {},
            "trade_mode": trade_mode,
            "default_direction": default_direction,
        },
        strategy_type,
        timeout,
    )


def compute_per_bar_signals_isolated(
    df: pd.DataFrame,
    strategy_type: str,
    params: dict,
    *,
    warmup: int,
    trade_mode: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> "DirectionalSignals | None":
    """Run the per-bar adapter (``_signals_from_per_bar`` — walks ``generate_signal``
    over a trailing window) in the isolated worker, for a strategy with NO vectorized
    ``generate_signals``. Returns the DirectionalSignals, or ``None`` when the strategy
    has no usable/pure per-bar method (caller falls back to the legacy slow path),
    byte-identical to the in-process adapter."""
    return _request_signals(
        df,
        {
            "mode": "per_bar",
            "strategy_type": strategy_type,
            "params": params or {},
            "trade_mode": trade_mode,
            "warmup": int(warmup),
        },
        strategy_type,
        timeout,
    )


def validate_custom_module_isolated(
    module_name: str, *, timeout: int = VALIDATE_TIMEOUT_SECONDS
) -> dict:
    """Validate a custom strategy module OUT-OF-PROCESS and return its metadata dict.

    Spawns a one-shot, secret-free, network-denied, FS-confined, resource-capped child
    that imports the module, builds the probe, certifies, and lookahead-scans it — so
    none of that untrusted code runs in the trusted parent. Returns
    ``{ok, type_name, default_params, canonical_params, asset, certified, cert_error,
    lookahead_blocked, lookahead_reason}`` on success, or ``{ok: False, error}``.
    Raises :class:`StrategyWorkerError` on timeout / no result (fail closed)."""
    with tempfile.TemporaryDirectory(prefix="forven_validate_") as tmp:
        workdir = Path(tmp)
        (workdir / "request.json").write_text(
            json.dumps({"module_name": str(module_name)}), encoding="utf-8"
        )
        env = _build_worker_env()
        proc = subprocess.Popen(
            [PYTHON_EXE, "-m", "forven.sandbox.strategy_worker", "--validate", str(workdir)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
            preexec_fn=(None if IS_WINDOWS else _build_posix_preexec(PERSISTENT_MAX_MEMORY_MB)),
        )
        job = kernel32 = None
        if IS_WINDOWS:
            job, kernel32 = _create_windows_job_object(PERSISTENT_MAX_MEMORY_MB)
            if job and kernel32:
                _assign_pid_to_job(job, kernel32, proc.pid)
        try:
            _, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            _close_job(job, kernel32)
            raise StrategyWorkerError(
                f"isolated validation of {module_name!r} timed out after {timeout}s"
            )
        finally:
            _close_job(job, kernel32)

        result_path = workdir / "result.json"
        if not result_path.exists():
            tail = (stderr or "").strip()[-1500:]
            raise StrategyWorkerError(
                f"isolated validation of {module_name!r} produced no result"
                + (f": {tail}" if tail else "")
            )
        try:
            return json.loads(result_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise StrategyWorkerError(
                f"isolated validation of {module_name!r} returned malformed result: {exc}"
            ) from exc


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
    if len(sys.argv) > 2 and sys.argv[1] == "--validate":
        raise SystemExit(_run_validate(Path(sys.argv[2])))
    _wd = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    raise SystemExit(_run_worker(_wd))
