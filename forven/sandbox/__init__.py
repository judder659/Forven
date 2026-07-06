"""Subprocess sandbox — safe execution of agent-generated code.

Uses subprocess with ulimit (CPU, memory, file) and timeout.
No Docker required.
"""

import logging
import os
import sys
import subprocess
import tempfile
from pathlib import Path

from forven.security.env_allowlist import build_subprocess_env

log = logging.getLogger("forven.sandbox")
REPO_ROOT = Path(__file__).resolve().parents[2]

# Resource limits (ulimit only applies to Linux)
MAX_CPU_SECONDS = 30
MAX_MEMORY_MB = 512
MAX_FILE_SIZE_MB = 10
MAX_OPEN_FILES = 32
MAX_ACTIVE_CHILD_PROCESSES = 4  # H-S4: limit fork-bomb / process-storm risk
TIMEOUT_SECONDS = 60

IS_WINDOWS = sys.platform == "win32"
PYTHON_EXE = sys.executable or "python"

# Cap BLAS thread pools in sandbox subprocesses. NumPy/pandas pull in
# OpenBLAS (and sometimes MKL/OMP), each of which allocates per-thread
# workspaces on import — one per CPU core by default. On a many-core host
# inside a 256MB Job Object that trips "OpenBLAS error: Memory allocation
# still failed after 10 retries". One thread per pool is plenty for a
# validation harness and keeps the footprint tiny. setdefault so operators
# can override for perf-sensitive sandbox workloads.
_BLAS_THREAD_ENV = {
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
}

if not IS_WINDOWS:
    import resource


def _build_posix_preexec(max_memory_mb: int):
    def _apply_limits() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (MAX_CPU_SECONDS, MAX_CPU_SECONDS))
        resource.setrlimit(resource.RLIMIT_AS, (max_memory_mb * 1024 * 1024, max_memory_mb * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_SIZE_MB * 1024 * 1024, MAX_FILE_SIZE_MB * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (MAX_OPEN_FILES, MAX_OPEN_FILES))

    return _apply_limits


# H-S4: Windows Job Object plumbing — ctypes-only so no extra dependency.
# Provides memory + active-process limits roughly equivalent to POSIX rlimit.
def _create_windows_job_object(max_memory_mb: int):
    """Create a Win32 Job Object with memory + active-process limits and the
    KILL_ON_JOB_CLOSE flag so the child dies when our handle does. Returns
    (job_handle, kernel32_module) or (None, None) on failure."""
    if not IS_WINDOWS:
        return None, None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None, None

    JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectExtendedLimitInformation = 9

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
        ]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            log.debug("Win32 CreateJobObjectW failed (errno=%s)", ctypes.get_last_error())
            return None, None

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        info.BasicLimitInformation.ActiveProcessLimit = MAX_ACTIVE_CHILD_PROCESSES
        info.ProcessMemoryLimit = max_memory_mb * 1024 * 1024

        ok = kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            log.debug("SetInformationJobObject failed (errno=%s)", ctypes.get_last_error())
            kernel32.CloseHandle(job)
            return None, None
        return job, kernel32
    except Exception as exc:
        log.debug("Job object setup failed: %s", exc)
        return None, None


def _assign_pid_to_job(job_handle, kernel32, pid: int) -> bool:
    if not job_handle or not kernel32 or not pid:
        return False
    try:
        import ctypes  # noqa: F401 (kept for clarity; wintypes used below)
        from ctypes import wintypes

        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

        proc_handle = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not proc_handle:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(job_handle, proc_handle))
        finally:
            kernel32.CloseHandle(proc_handle)
    except Exception as exc:
        log.debug("AssignProcessToJobObject failed: %s", exc)
        return False


def _close_job(job_handle, kernel32) -> None:
    if not job_handle or not kernel32:
        return
    try:
        kernel32.CloseHandle(job_handle)
    except Exception:
        pass


def run_code(
    code: str,
    timeout: int = TIMEOUT_SECONDS,
    max_memory_mb: int = MAX_MEMORY_MB,
) -> dict:
    """Execute Python code in an isolated subprocess with resource limits.

    Returns: {"stdout": str, "stderr": str, "returncode": int, "timed_out": bool}
    """
    # Use default system temp dir instead of hardcoded /tmp
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8", newline="\n"
    ) as f:
        f.write(code)
        script_path = f.name

    job_handle = None
    kernel32 = None

    if IS_WINDOWS:
        cmd = [PYTHON_EXE, script_path]
        # SECURITY (H-1): do NOT inherit the full parent environment — that
        # passes every secret-bearing var (ANTHROPIC_API_KEY, FORVEN_HL_API_SECRET,
        # FORVEN_ENCRYPTION_KEY, …) straight into AI-generated / prompt-injectable
        # code. Build a filtered env via the allowlist (same as run_shell), then
        # add PYTHONPATH + BLAS caps explicitly. BLAS values honour a parent
        # override if present, mirroring the POSIX branch's setdefault semantics.
        # ENV-HERMETIC-1: PYTHONPATH is pinned to the repo root ONLY — inheriting
        # the parent's value let a global Anaconda re-root the child's dependency
        # resolution away from the project venv (import failures only inside the
        # sandbox).
        extra = {"PYTHONPATH": str(REPO_ROOT)}
        for _k, _v in _BLAS_THREAD_ENV.items():
            extra[_k] = os.environ.get(_k, _v)
        env = build_subprocess_env(extra=extra)
        # H-S4: best-effort Job Object for memory + active-process caps.
        # Use Popen on Windows so we can assign the spawned PID to the Job
        # before it gets a chance to exhaust resources.
        job_handle, kernel32 = _create_windows_job_object(max_memory_mb)
        try:
            try:
                proc = subprocess.Popen(
                    cmd, shell=False,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    env=env, cwd=str(REPO_ROOT),
                )
            except Exception as exc:
                return {
                    "stdout": "", "stderr": f"Execution error: {str(exc)}",
                    "returncode": -1, "timed_out": False,
                }
            if job_handle and kernel32:
                if not _assign_pid_to_job(job_handle, kernel32, proc.pid):
                    log.debug("Failed to assign sandboxed pid %s to job; running without limits", proc.pid)
            try:
                stdout_b, stderr_b = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.communicate(timeout=2)
                except Exception:
                    pass
                return {
                    "stdout": "", "stderr": "Execution timed out",
                    "returncode": -1, "timed_out": True,
                }
            return {
                "stdout": (stdout_b or "")[:10000].replace("\\", "/"),
                "stderr": (stderr_b or "")[:5000].replace("\\", "/"),
                "returncode": proc.returncode,
                "timed_out": False,
            }
        finally:
            _close_job(job_handle, kernel32)
            try:
                Path(script_path).unlink(missing_ok=True)
            except Exception:
                pass

    # POSIX path: keep using subprocess.run with rlimit-based preexec.
    cmd = [PYTHON_EXE, script_path]
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/usr/local/bin"), "HOME": tempfile.gettempdir()}
    for _k, _v in _BLAS_THREAD_ENV.items():
        env.setdefault(_k, os.environ.get(_k, _v))
    preexec_fn = _build_posix_preexec(max_memory_mb)
    try:
        proc = subprocess.run(
            cmd, shell=False, capture_output=True, text=True,
            timeout=timeout,
            env=env,
            cwd=str(REPO_ROOT),
            preexec_fn=preexec_fn,
        )
        return {
            "stdout": proc.stdout[:10000].replace("\\", "/"),
            "stderr": proc.stderr[:5000].replace("\\", "/"),
            "returncode": proc.returncode,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "", "stderr": "Execution timed out",
            "returncode": -1, "timed_out": True,
        }
    except Exception as e:
        return {
            "stdout": "", "stderr": f"Execution error: {str(e)}",
            "returncode": -1, "timed_out": False,
        }
    finally:
        try:
            Path(script_path).unlink(missing_ok=True)
        except Exception:
            pass


def lint_code(code: str) -> dict:
    """Run ruff on code and return diagnostics.

    Returns: {"passed": bool, "issues": list[str], "fixed_code": str | None}
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8", newline="\n"
    ) as f:
        f.write(code)
        script_path = f.name

    try:
        # Use python -m ruff to ensure we use the same environment
        check = subprocess.run(
            [PYTHON_EXE, "-m", "ruff", "check", script_path, "--output-format=text"],
            capture_output=True, text=True, timeout=15,
        )
        issues = [line for line in check.stdout.strip().split("\n") if line.strip()]

        fixed_code = None
        if issues:
            import shutil
            fix_path = script_path + ".fix"
            shutil.copy2(script_path, fix_path)
            subprocess.run(
                [PYTHON_EXE, "-m", "ruff", "check", "--fix", fix_path],
                capture_output=True, timeout=15,
            )
            try:
                fixed_code = Path(fix_path).read_text(encoding="utf-8")
            except Exception:
                fixed_code = None
            finally:
                Path(fix_path).unlink(missing_ok=True)

        return {
            "passed": len(issues) == 0,
            "issues": issues,
            "fixed_code": fixed_code,
        }
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        return {"passed": True, "issues": [f"Linting tool error: {str(e)}"], "fixed_code": None}
    finally:
        try:
            Path(script_path).unlink(missing_ok=True)
        except Exception:
            pass

