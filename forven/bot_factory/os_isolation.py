"""Run bot subprocesses in a low-integrity sandbox (Windows, on by default).

Bots never receive the master encryption key (#117), but they run as the
operator's own Windows user, so a compromised bot could otherwise read the key
file and rewrite Forven's code. Without asking anyone to create an account,
each bot is started at Windows' *low* integrity level, the same mechanism
browsers use for their sandboxes:

* A low-integrity process cannot write anything labelled at the normal
  (medium) level, which covers Forven's code, Python, and the operator's
  profile. Forven's data folder is labelled low once, so bots keep using the
  database, market data and their memory exactly as before.
* The master key's folder (and a legacy in-home key, and ``.env`` files) are
  labelled *no-read-up*, so a bot cannot read them either.
* Bots get a private, writable profile and temp folder under
  ``FORVEN_HOME/bot-runtime``.

The sandbox checks itself once per API process by starting a short probe in
it. If the probe cannot do what a bot needs (load Forven, write the database),
or the sandbox cannot be created at all, bots start as before and the activity
log says why, so the sandbox never breaks a working install. Set
``FORVEN_BOT_SANDBOX=0`` to turn it off. This module deliberately has no
Forven imports; the bot manager passes in the paths it needs.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

SANDBOX_ENV = "FORVEN_BOT_SANDBOX"
RUNTIME_DIR_NAME = "bot-runtime"
_LABEL_MARKER = ".low-integrity-v1"
_PROBE_MARKER = "FORVEN_PROBE "


class SandboxUnavailable(RuntimeError):
    """The low-integrity sandbox cannot be used on this machine right now."""


@dataclass
class SandboxStatus:
    usable: bool
    problems: list[str] = field(default_factory=list)


def _is_windows() -> bool:
    return os.name == "nt"


def sandbox_enabled() -> bool:
    """On by default on Windows; ``FORVEN_BOT_SANDBOX=0`` turns it off."""
    if not _is_windows():
        return False
    return str(os.environ.get(SANDBOX_ENV, "1")).strip().lower() not in {"0", "false", "off", "no"}


def bot_runtime_dir(forven_home: Path) -> Path:
    return Path(forven_home) / RUNTIME_DIR_NAME


def sandbox_env(env: dict[str, str], forven_home: Path) -> dict[str, str]:
    """Point the bot's profile and temp folders at its writable runtime folder.

    The operator's own USERPROFILE / APPDATA / TEMP are medium-integrity, so a
    sandboxed bot could not write there.
    """
    runtime = bot_runtime_dir(forven_home)
    home = runtime / "home"
    tmp = runtime / "tmp"
    roaming = home / "AppData" / "Roaming"
    local = home / "AppData" / "Local"
    for folder in (tmp, roaming, local):
        folder.mkdir(parents=True, exist_ok=True)
    env = dict(env)
    for var in ("HOMEDRIVE", "HOMEPATH"):
        env.pop(var, None)
    env.update(
        {
            "USERPROFILE": str(home),
            "HOME": str(home),
            "APPDATA": str(roaming),
            "LOCALAPPDATA": str(local),
            "TEMP": str(tmp),
            "TMP": str(tmp),
        }
    )
    return env


# ── Preparing the file system and checking the sandbox ──────────────

_ready_lock = threading.Lock()
_ready: SandboxStatus | None = None


def ensure_sandbox_ready(
    forven_home: Path,
    protected: list[Path],
    db_path: Path,
    repo_root: Path,
) -> SandboxStatus:
    """Label the folders and run the self-check, once per process.

    ``usable`` is False only when bots could not work inside the sandbox; a
    probe that could still read a protected file is reported in ``problems``
    but keeps the sandbox on, since it still blocks writes to code.
    """
    global _ready
    with _ready_lock:
        if _ready is not None:
            return _ready
        try:
            _label_data_folder(Path(forven_home))
            for path in protected:
                _protect_from_low_readers(Path(path))
            _ready = _judge_probe(_run_probe(forven_home, protected, db_path, repo_root))
        except Exception as exc:
            logger.warning("Bot sandbox unavailable", exc_info=True)
            _ready = SandboxStatus(False, [f"The sandbox could not be set up: {exc}"])
        return _ready


def _reset_for_tests() -> None:
    global _ready
    with _ready_lock:
        _ready = None


def _label_data_folder(forven_home: Path) -> None:
    """Let low-integrity bots write Forven's data folder (done once per install).

    New files inherit the label from their folder, so only existing files need
    the one-off recursive pass.
    """
    runtime = bot_runtime_dir(forven_home)
    runtime.mkdir(parents=True, exist_ok=True)
    marker = runtime / _LABEL_MARKER
    if marker.exists():
        return
    result = subprocess.run(
        ["icacls", str(forven_home), "/setintegritylevel", "(OI)(CI)L", "/T", "/C", "/Q"],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise SandboxUnavailable(f"Could not label {forven_home}: {(result.stderr or result.stdout).strip()}")
    marker.write_text("Forven data folder labelled for low-integrity bots.\n", encoding="utf-8")


def _protect_from_low_readers(path: Path) -> None:
    """Mark a file or folder (and its contents) unreadable from low integrity."""
    if not path.exists():
        return
    if path.is_dir():
        _set_mandatory_label(path, "S:(ML;OICI;NRNWNX;;;ME)")
        for child in path.iterdir():
            if child.is_file():
                _set_mandatory_label(child, "S:(ML;;NRNWNX;;;ME)")
    else:
        _set_mandatory_label(path, "S:(ML;;NRNWNX;;;ME)")


# The probe runs inside the sandbox, so it must not need anything a bot lacks.
PROBE_SCRIPT = r"""
import json, os, sqlite3, subprocess, sys
db, repo, secrets = sys.argv[1], sys.argv[2], sys.argv[3:]
out = {}
try:
    groups = subprocess.run(["whoami", "/groups"], capture_output=True, text=True, timeout=15).stdout
    out["low_integrity"] = "S-1-16-4096" in groups
except Exception as exc:
    out["low_integrity"] = repr(exc)
try:
    import forven.bot_factory.runner  # noqa: F401
    out["imports_forven"] = True
except Exception as exc:
    out["imports_forven"] = repr(exc)
try:
    conn = sqlite3.connect(db, timeout=10)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("ROLLBACK")
    conn.close()
    out["writes_database"] = True
except Exception as exc:
    out["writes_database"] = repr(exc)
probe = os.path.join(repo, ".forven-sandbox-probe")
try:
    with open(probe, "w") as fh:
        fh.write("x")
    os.remove(probe)
    out["code_writable"] = True
except OSError:
    out["code_writable"] = False
readable = []
for path in secrets:
    try:
        with open(path, "rb") as fh:
            fh.read(1)
        readable.append(path)
    except OSError:
        pass
out["readable_secrets"] = readable
print("FORVEN_PROBE " + json.dumps(out), flush=True)
"""


def _run_probe(forven_home: Path, protected: list[Path], db_path: Path, repo_root: Path) -> str:
    secret_files: list[str] = []
    for path in protected:
        path = Path(path)
        if path.is_dir():
            secret_files += [str(child) for child in path.iterdir() if child.is_file()]
        elif path.exists():
            secret_files.append(str(path))
    log_path = bot_runtime_dir(forven_home) / "sandbox-check.log"
    log_path.write_text("", encoding="utf-8")
    passthrough = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}
    env = sandbox_env({k: v for k, v in os.environ.items() if k.upper() in passthrough}, forven_home)
    env["FORVEN_HOME"] = str(forven_home)
    env["FORVEN_NO_MASTER_KEY"] = "1"
    process = spawn_sandboxed(
        [sys.executable, "-c", PROBE_SCRIPT, str(db_path), str(repo_root), *secret_files],
        env=env, cwd=repo_root, log_path=log_path,
    )
    try:
        process.wait(90)
    except subprocess.TimeoutExpired:
        process.kill()
        raise SandboxUnavailable("The sandbox check did not finish within 90s")
    finally:
        process.close()
    return log_path.read_text(encoding="utf-8", errors="replace")


def _judge_probe(output: str) -> SandboxStatus:
    line = next((ln for ln in output.splitlines() if ln.startswith(_PROBE_MARKER)), "")
    if not line:
        tail = output.strip()[-600:] or "(no output)"
        return SandboxStatus(False, [f"The sandbox check did not run: {tail}"])
    result = json.loads(line[len(_PROBE_MARKER):])
    blocking, warnings = [], []
    if result.get("low_integrity") is not True:
        blocking.append("The check did not run at low integrity")
    if result.get("imports_forven") is not True:
        blocking.append(f"A sandboxed bot cannot load Forven: {result.get('imports_forven')}")
    if result.get("writes_database") is not True:
        blocking.append(f"A sandboxed bot cannot write the database: {result.get('writes_database')}")
    if result.get("code_writable") is not False:
        warnings.append("A sandboxed bot can still write Forven's code folder")
    for path in result.get("readable_secrets") or []:
        warnings.append(f"A sandboxed bot can still read {path}")
    return SandboxStatus(not blocking, blocking + warnings)


# ── Windows implementation ──────────────────────────────────────────

_MAXIMUM_ALLOWED = 0x02000000
_TOKEN_DUPLICATE = 0x2
_TOKEN_QUERY = 0x8
_TOKEN_ADJUST_DEFAULT = 0x80
_TOKEN_ASSIGN_PRIMARY = 0x1
_SECURITY_IMPERSONATION = 2
_TOKEN_PRIMARY = 1
_TOKEN_INTEGRITY_LEVEL = 25
_SE_GROUP_INTEGRITY = 0x20
_LOW_INTEGRITY_SID = "S-1-16-4096"
_SE_FILE_OBJECT = 1
_LABEL_SECURITY_INFORMATION = 0x10
_SDDL_REVISION_1 = 1
_CREATE_NEW_PROCESS_GROUP = 0x200
_CREATE_UNICODE_ENVIRONMENT = 0x400
_CREATE_NO_WINDOW = 0x08000000
_EXTENDED_STARTUPINFO_PRESENT = 0x80000
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x20002
_STARTF_USESTDHANDLES = 0x100
_GENERIC_READ = 0x80000000
_FILE_APPEND_DATA = 0x4
_SYNCHRONIZE = 0x100000
_FILE_SHARE_ALL = 0x7
_OPEN_EXISTING = 3
_OPEN_ALWAYS = 4
_FILE_ATTRIBUTE_NORMAL = 0x80
_STILL_ACTIVE = 259
_WAIT_TIMEOUT = 0x102
_INFINITE = 0xFFFFFFFF
_INVALID_HANDLE_VALUE = -1


@lru_cache(maxsize=1)
def _win32() -> dict:
    """Bind the kernel32/advapi32 calls used here (Windows only)."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", wintypes.LPVOID)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]

    class TOKEN_MANDATORY_LABEL(ctypes.Structure):
        _fields_ = [("Label", SID_AND_ATTRIBUTES)]

    H, D, B, P = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.LPVOID
    kernel32.GetCurrentProcess.restype = H
    kernel32.CreateFileW.restype = H
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, D, D, ctypes.POINTER(SECURITY_ATTRIBUTES), D, D, H]
    kernel32.InitializeProcThreadAttributeList.argtypes = [P, D, D, ctypes.POINTER(ctypes.c_size_t)]
    kernel32.UpdateProcThreadAttribute.argtypes = [P, D, ctypes.c_size_t, P, ctypes.c_size_t, P, P]
    kernel32.DeleteProcThreadAttributeList.argtypes = [P]
    kernel32.TerminateProcess.argtypes = [H, wintypes.UINT]
    kernel32.GetExitCodeProcess.argtypes = [H, ctypes.POINTER(D)]
    kernel32.WaitForSingleObject.restype = D
    kernel32.WaitForSingleObject.argtypes = [H, D]
    kernel32.CloseHandle.argtypes = [H]
    kernel32.LocalFree.argtypes = [P]
    advapi32.OpenProcessToken.argtypes = [H, D, ctypes.POINTER(H)]
    advapi32.DuplicateTokenEx.argtypes = [H, D, P, ctypes.c_int, ctypes.c_int, ctypes.POINTER(H)]
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(P)]
    advapi32.GetLengthSid.restype = D
    advapi32.GetLengthSid.argtypes = [P]
    advapi32.SetTokenInformation.argtypes = [H, ctypes.c_int, P, D]
    advapi32.CreateProcessAsUserW.argtypes = [
        H, wintypes.LPCWSTR, wintypes.LPWSTR, P, P, B, D, P, wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOEXW), ctypes.POINTER(PROCESS_INFORMATION),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, D, ctypes.POINTER(P), P,
    ]
    advapi32.GetSecurityDescriptorSacl.argtypes = [P, ctypes.POINTER(B), ctypes.POINTER(P), ctypes.POINTER(B)]
    advapi32.SetNamedSecurityInfoW.restype = D
    advapi32.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, D, P, P, P, P]

    return {
        "ctypes": ctypes, "wintypes": wintypes, "kernel32": kernel32, "advapi32": advapi32,
        "SECURITY_ATTRIBUTES": SECURITY_ATTRIBUTES, "STARTUPINFOEXW": STARTUPINFOEXW,
        "PROCESS_INFORMATION": PROCESS_INFORMATION, "TOKEN_MANDATORY_LABEL": TOKEN_MANDATORY_LABEL,
    }


def _win_error(message: str) -> SandboxUnavailable:
    ctypes = _win32()["ctypes"]
    return SandboxUnavailable(f"{message}: {ctypes.WinError(ctypes.get_last_error())}")


def _set_mandatory_label(path: Path, sddl: str) -> None:
    w = _win32()
    ctypes, wintypes, kernel32, advapi32 = w["ctypes"], w["wintypes"], w["kernel32"], w["advapi32"]
    descriptor = wintypes.LPVOID()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None,
    ):
        raise _win_error(f"Bad label {sddl}")
    try:
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        sacl = wintypes.LPVOID()
        if not advapi32.GetSecurityDescriptorSacl(
            descriptor, ctypes.byref(present), ctypes.byref(sacl), ctypes.byref(defaulted),
        ):
            raise _win_error("Cannot read label")
        status = advapi32.SetNamedSecurityInfoW(
            str(path), _SE_FILE_OBJECT, _LABEL_SECURITY_INFORMATION, None, None, None, sacl,
        )
        if status != 0:
            raise SandboxUnavailable(f"Cannot label {path}: {ctypes.WinError(status)}")
    finally:
        kernel32.LocalFree(descriptor)


def _low_integrity_token() -> int:
    """A primary copy of this process's token, lowered to low integrity."""
    w = _win32()
    ctypes, wintypes, kernel32, advapi32 = w["ctypes"], w["wintypes"], w["kernel32"], w["advapi32"]
    own = wintypes.HANDLE()
    access = _TOKEN_DUPLICATE | _TOKEN_QUERY | _TOKEN_ADJUST_DEFAULT | _TOKEN_ASSIGN_PRIMARY
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), access, ctypes.byref(own)):
        raise _win_error("Cannot open this process's token")
    token = wintypes.HANDLE()
    sid = wintypes.LPVOID()
    try:
        if not advapi32.DuplicateTokenEx(
            own, _MAXIMUM_ALLOWED, None, _SECURITY_IMPERSONATION, _TOKEN_PRIMARY, ctypes.byref(token),
        ):
            raise _win_error("Cannot copy this process's token")
        if not advapi32.ConvertStringSidToSidW(_LOW_INTEGRITY_SID, ctypes.byref(sid)):
            raise _win_error("Cannot build the low-integrity SID")
        label = w["TOKEN_MANDATORY_LABEL"]()
        label.Label.Sid = sid
        label.Label.Attributes = _SE_GROUP_INTEGRITY
        size = ctypes.sizeof(label) + advapi32.GetLengthSid(sid)
        if not advapi32.SetTokenInformation(token, _TOKEN_INTEGRITY_LEVEL, ctypes.byref(label), size):
            raise _win_error("Cannot lower the token to low integrity")
        return token.value
    except Exception:
        if token.value:
            kernel32.CloseHandle(token)
        raise
    finally:
        kernel32.CloseHandle(own)
        if sid.value:
            kernel32.LocalFree(sid)


def _environment_block(env: dict[str, str]) -> str:
    """A CREATE_UNICODE_ENVIRONMENT block: sorted ``K=V\\0`` entries, then ``\\0``."""
    entries = sorted(((str(k), str(v)) for k, v in env.items() if k), key=lambda kv: kv[0].upper())
    return "".join(f"{k}={v}\0" for k, v in entries) + "\0"


class SandboxedProcess:
    """The subset of ``subprocess.Popen`` the bot manager uses, over a raw handle.

    The bot still runs as the operator's user, so PID-based control (psutil
    kill, liveness checks) keeps working after an API restart.
    """

    def __init__(self, pid: int, process_handle: int):
        self.pid = pid
        self._handle = process_handle
        self.returncode: int | None = None

    def close(self) -> None:
        if self._handle:
            _win32()["kernel32"].CloseHandle(self._handle)
            self._handle = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def poll(self) -> int | None:
        if self.returncode is not None or not self._handle:
            return self.returncode
        w = _win32()
        code = w["wintypes"].DWORD()
        if w["kernel32"].GetExitCodeProcess(self._handle, w["ctypes"].byref(code)) and code.value != _STILL_ACTIVE:
            self.returncode = int(code.value)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        millis = _INFINITE if timeout is None else max(0, int(timeout * 1000))
        if _win32()["kernel32"].WaitForSingleObject(self._handle, millis) == _WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(f"bot pid {self.pid}", timeout)
        code = self.poll()
        return 0 if code is None else code

    def kill(self) -> None:
        if self._handle:
            _win32()["kernel32"].TerminateProcess(self._handle, 1)

    terminate = kill


def spawn_sandboxed(argv: list[str], *, env: dict[str, str], cwd: Path, log_path: Path) -> SandboxedProcess:
    """Start ``argv`` at low integrity, logging stdout/stderr to ``log_path``.

    Only the log and NUL handles are inherited (as ``Popen(close_fds=True)``
    does), and no console window is created.
    """
    if not _is_windows():
        raise SandboxUnavailable("The bot sandbox is only available on Windows")
    w = _win32()
    ctypes, wintypes, kernel32, advapi32 = w["ctypes"], w["wintypes"], w["kernel32"], w["advapi32"]
    SA = w["SECURITY_ATTRIBUTES"]

    inherit = SA(ctypes.sizeof(SA), None, True)
    log_handle = kernel32.CreateFileW(
        str(log_path), _FILE_APPEND_DATA | _SYNCHRONIZE, _FILE_SHARE_ALL, ctypes.byref(inherit),
        _OPEN_ALWAYS, _FILE_ATTRIBUTE_NORMAL, None,
    )
    if not log_handle or log_handle == _INVALID_HANDLE_VALUE:
        raise _win_error(f"Cannot open bot log {log_path}")
    null_handle = kernel32.CreateFileW(
        "NUL", _GENERIC_READ, _FILE_SHARE_ALL, ctypes.byref(inherit), _OPEN_EXISTING, 0, None,
    )
    token = None
    attributes = None
    info = w["PROCESS_INFORMATION"]()
    try:
        if not null_handle or null_handle == _INVALID_HANDLE_VALUE:
            raise _win_error("Cannot open NUL")
        token = _low_integrity_token()

        size = ctypes.c_size_t()
        kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not kernel32.InitializeProcThreadAttributeList(buffer, 1, 0, ctypes.byref(size)):
            raise _win_error("Cannot build the handle list")
        attributes = buffer
        handles = (wintypes.HANDLE * 2)(log_handle, null_handle)
        if not kernel32.UpdateProcThreadAttribute(
            attributes, 0, _PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles, ctypes.sizeof(handles), None, None,
        ):
            raise _win_error("Cannot set the handle list")

        startup = w["STARTUPINFOEXW"]()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = null_handle
        startup.StartupInfo.hStdOutput = log_handle
        startup.StartupInfo.hStdError = log_handle
        startup.lpAttributeList = ctypes.cast(attributes, wintypes.LPVOID)

        block = _environment_block(env)
        env_buffer = (ctypes.c_wchar * len(block))(*block)
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        flags = (
            _CREATE_NEW_PROCESS_GROUP | _CREATE_UNICODE_ENVIRONMENT | _CREATE_NO_WINDOW
            | _EXTENDED_STARTUPINFO_PRESENT
        )
        if not advapi32.CreateProcessAsUserW(
            token, None, command_line, None, None, True, flags,
            ctypes.cast(env_buffer, wintypes.LPVOID), str(cwd), ctypes.byref(startup), ctypes.byref(info),
        ):
            raise _win_error("Cannot start the sandboxed process")
        return SandboxedProcess(int(info.dwProcessId), info.hProcess)
    finally:
        if info.hThread:
            kernel32.CloseHandle(info.hThread)
        if attributes is not None:
            kernel32.DeleteProcThreadAttributeList(attributes)
        if token:
            kernel32.CloseHandle(token)
        for handle in (log_handle, null_handle):
            if handle and handle != _INVALID_HANDLE_VALUE:
                kernel32.CloseHandle(handle)
