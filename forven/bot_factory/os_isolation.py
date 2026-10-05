"""Run bot subprocesses as a separate, low-privilege OS account (opt-in).

Bots never receive the master encryption key (#117), but by default they still
run as the operator's own Windows user, so a hostile or compromised bot could
read the key file, the rest of the operator's profile, and Forven's own code.
When an operator sets up a dedicated account (``scripts/setup-bot-user.ps1``),
every bot spawn runs as that account instead:

* The account's password lives next to the master key, outside FORVEN_HOME and
  outside the shared database, encrypted with that key. A bot can therefore
  neither read it nor delete it to quietly turn isolation off: a bot can write
  the database, but nothing here reads the database.
* Bots get a private profile under ``FORVEN_HOME/bot-runtime`` instead of the
  operator's USERPROFILE / APPDATA / TEMP.
* Each bot is placed in a named Job Object created by the API process before
  the bot runs a single instruction. The bot cannot leave the job or change its
  permissions, so the API can always terminate the bot and anything it spawned,
  even after an API restart and even though the bot's process belongs to
  another user.

Isolation is fail-closed: once configured, a bot that cannot be started as the
account is not started at all. Windows only for now; the configuration is
refused elsewhere.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable
from functools import lru_cache
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

ACCOUNT_FILE_NAME = "bot-account.json"
RUNTIME_DIR_NAME = "bot-runtime"
_JOB_NAME_PREFIX = "Local\\forven-bot-"


class BotIsolationError(ValueError):
    """Bot isolation is configured but a bot cannot be run inside it.

    A ValueError so the bot routes report it like any other refused start.
    """


@dataclass(frozen=True)
class BotOsAccount:
    username: str
    password: str = field(repr=False)

    @property
    def domain_and_user(self) -> tuple[str, str]:
        """Split ``DOMAIN\\user``; a bare name is a local account (``.``)."""
        if "\\" in self.username:
            domain, user = self.username.split("\\", 1)
            return domain or ".", user
        return ".", self.username


def _is_windows() -> bool:
    return os.name == "nt"


def read_account_record(path: Path, decrypt: Callable[[str], str]) -> BotOsAccount | None:
    """The account recorded at ``path``, or None when isolation is off (the default).

    Raises BotIsolationError when a record exists but cannot be used, so a
    damaged record never silently downgrades bots to the operator's account.
    ``decrypt`` is passed in so this module stays free of Forven imports.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        username = str(data.get("username") or "").strip()
        password = decrypt(str(data.get("password") or ""))
    except Exception as exc:
        raise BotIsolationError(f"Bot account record at {path} is unreadable: {exc}") from exc
    if not username or not password:
        raise BotIsolationError(f"Bot account record at {path} is incomplete")
    return BotOsAccount(username=username, password=password)


def bot_runtime_dir(forven_home: Path) -> Path:
    return Path(forven_home) / RUNTIME_DIR_NAME


def apply_account_env(env: dict[str, str], account: BotOsAccount, forven_home: Path) -> dict[str, str]:
    """Point the bot's profile and temp folders at its own runtime directory.

    The parent's USERPROFILE/APPDATA/TEMP belong to the operator and are not
    readable by the bot account, so the bot gets a private home inside
    FORVEN_HOME (which the setup script grants it).
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
            "USERNAME": account.domain_and_user[1],
        }
    )
    return env


def job_name(bot_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(bot_id))
    return f"{_JOB_NAME_PREFIX}{safe}"


# ── Windows implementation ──────────────────────────────────────────

_LOGON_WITH_PROFILE = 0x1
_CREATE_SUSPENDED = 0x4
_CREATE_NEW_PROCESS_GROUP = 0x200
_CREATE_UNICODE_ENVIRONMENT = 0x400
_CREATE_NO_WINDOW = 0x08000000
_STARTF_USESTDHANDLES = 0x100
_GENERIC_READ = 0x80000000
_FILE_APPEND_DATA = 0x4
_SYNCHRONIZE = 0x100000
_FILE_SHARE_ALL = 0x7
_OPEN_EXISTING = 3
_OPEN_ALWAYS = 4
_FILE_ATTRIBUTE_NORMAL = 0x80
_STILL_ACTIVE = 259
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x102
_INFINITE = 0xFFFFFFFF
_JOB_OBJECT_TERMINATE = 0x8
_JOB_OBJECT_QUERY = 0x4
_JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
_ERROR_ALREADY_EXISTS = 183
_INVALID_HANDLE_VALUE = -1


@lru_cache(maxsize=1)
def _win32():
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

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES),
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.OpenJobObjectW.restype = wintypes.HANDLE
    kernel32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi32.CreateProcessWithLogonW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, wintypes.LPVOID,
        wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION),
    ]

    return ctypes, wintypes, kernel32, advapi32, SECURITY_ATTRIBUTES, STARTUPINFOW, PROCESS_INFORMATION


def _environment_block(env: dict[str, str]) -> str:
    """A CREATE_UNICODE_ENVIRONMENT block: sorted ``K=V\\0`` entries, then ``\\0``."""
    entries = sorted(((str(k), str(v)) for k, v in env.items() if k), key=lambda kv: kv[0].upper())
    return "".join(f"{k}={v}\0" for k, v in entries) + "\0"


class AccountProcess:
    """The subset of ``subprocess.Popen`` the bot manager uses, over a raw handle."""

    def __init__(self, pid: int, process_handle: int, job_handle: int):
        self.pid = pid
        self._handle = process_handle
        self._job = job_handle
        self.returncode: int | None = None

    def close(self) -> None:
        """Release our handles (the bot keeps running; its job stays alive with it)."""
        _, _, kernel32, *_ = _win32()
        for attr in ("_handle", "_job"):
            handle = getattr(self, attr, None)
            if handle:
                kernel32.CloseHandle(handle)
                setattr(self, attr, None)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        ctypes, wintypes, kernel32, *_ = _win32()
        code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(self._handle, ctypes.byref(code)) and code.value != _STILL_ACTIVE:
            self.returncode = int(code.value)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        _, _, kernel32, *_ = _win32()
        millis = _INFINITE if timeout is None else max(0, int(timeout * 1000))
        if kernel32.WaitForSingleObject(self._handle, millis) == _WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(f"bot pid {self.pid}", timeout)
        code = self.poll()
        return 0 if code is None else code

    def kill(self) -> None:
        _, _, kernel32, *_ = _win32()
        # The whole job, so anything the bot spawned (including the venv
        # launcher's base interpreter) goes with it.
        if not kernel32.TerminateJobObject(self._job, 1):
            kernel32.TerminateProcess(self._handle, 1)

    terminate = kill


def spawn_as_account(
    account: BotOsAccount,
    bot_id: str,
    argv: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    log_path: Path,
) -> AccountProcess:
    """Start ``argv`` as ``account``, inside this bot's Job Object, logging to ``log_path``.

    The process is created suspended and only resumed once it is in the job, so
    it never runs outside the API's control. Any failure terminates it.
    """
    if not _is_windows():
        raise BotIsolationError("Running bots as a separate account is only supported on Windows")

    ctypes, wintypes, kernel32, advapi32, SA, STARTUPINFOW, PROCESS_INFORMATION = _win32()

    inherit = SA(ctypes.sizeof(SA), None, True)
    log_handle = kernel32.CreateFileW(
        str(log_path), _FILE_APPEND_DATA | _SYNCHRONIZE, _FILE_SHARE_ALL, ctypes.byref(inherit),
        _OPEN_ALWAYS, _FILE_ATTRIBUTE_NORMAL, None,
    )
    if not log_handle or log_handle == _INVALID_HANDLE_VALUE:
        raise BotIsolationError(f"Cannot open bot log {log_path}: {ctypes.WinError(ctypes.get_last_error())}")
    null_handle = kernel32.CreateFileW(
        "NUL", _GENERIC_READ, _FILE_SHARE_ALL, ctypes.byref(inherit), _OPEN_EXISTING, 0, None,
    )
    job = None
    info = PROCESS_INFORMATION()
    started = False
    try:
        ctypes.set_last_error(0)
        job = kernel32.CreateJobObjectW(None, job_name(bot_id))
        if not job:
            raise BotIsolationError(f"Cannot create job for bot {bot_id}: {ctypes.WinError(ctypes.get_last_error())}")
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            # Someone else holds this name (an earlier copy of the bot, or a
            # process squatting on it). Never adopt a job we did not create.
            raise BotIsolationError(f"A job for bot {bot_id} already exists; is the bot still running?")

        startup = STARTUPINFOW()
        startup.cb = ctypes.sizeof(STARTUPINFOW)
        startup.dwFlags = _STARTF_USESTDHANDLES
        startup.hStdInput = null_handle
        startup.hStdOutput = log_handle
        startup.hStdError = log_handle

        block = _environment_block(env)
        env_buffer = (ctypes.c_wchar * len(block))(*block)
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        domain, user = account.domain_and_user
        ok = advapi32.CreateProcessWithLogonW(
            user, domain, account.password, _LOGON_WITH_PROFILE, None, command_line,
            _CREATE_SUSPENDED | _CREATE_NEW_PROCESS_GROUP | _CREATE_UNICODE_ENVIRONMENT | _CREATE_NO_WINDOW,
            ctypes.cast(env_buffer, wintypes.LPVOID), str(cwd), ctypes.byref(startup), ctypes.byref(info),
        )
        if not ok:
            raise BotIsolationError(
                f"Cannot start bot {bot_id} as {account.username}: {ctypes.WinError(ctypes.get_last_error())}"
            )
        if not kernel32.AssignProcessToJobObject(job, info.hProcess):
            raise BotIsolationError(
                f"Cannot place bot {bot_id} in its job: {ctypes.WinError(ctypes.get_last_error())}"
            )
        if kernel32.ResumeThread(info.hThread) == 0xFFFFFFFF:
            raise BotIsolationError(f"Cannot resume bot {bot_id}: {ctypes.WinError(ctypes.get_last_error())}")
        started = True
        return AccountProcess(int(info.dwProcessId), info.hProcess, job)
    finally:
        if info.hThread:
            kernel32.CloseHandle(info.hThread)
        if not started:
            if info.hProcess:
                kernel32.TerminateProcess(info.hProcess, 1)
                kernel32.CloseHandle(info.hProcess)
            if job:
                kernel32.CloseHandle(job)
        for handle in (log_handle, null_handle):
            if handle and handle != _INVALID_HANDLE_VALUE:
                kernel32.CloseHandle(handle)


def terminate_bot_job(bot_id: str, pid: int | None = None) -> bool:
    """Kill an isolated bot and everything it spawned via its named job.

    Works after an API restart too, since the job is found by name and the API's
    user created it. With ``pid``, the job is only used when that process is in
    it, so a job someone else created under the bot's name ends nothing. False
    when there is no usable job (the bot was not isolated, or has already
    exited), so callers fall back to killing by PID.
    """
    if not _is_windows():
        return False
    ctypes, wintypes, kernel32, *_ = _win32()
    job = kernel32.OpenJobObjectW(_JOB_OBJECT_TERMINATE | _JOB_OBJECT_QUERY, False, job_name(bot_id))
    if not job:
        return False
    try:
        if pid is not None and int(pid) not in _job_process_ids(job):
            return False
        return bool(kernel32.TerminateJobObject(job, 1))
    finally:
        kernel32.CloseHandle(job)


def _job_process_ids(job: int, capacity: int = 256) -> set[int]:
    ctypes, wintypes, kernel32, *_ = _win32()
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]

    class JOBOBJECT_BASIC_PROCESS_ID_LIST(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", ctypes.c_size_t * capacity),
        ]

    info = JOBOBJECT_BASIC_PROCESS_ID_LIST()
    if not kernel32.QueryInformationJobObject(
        job, _JOB_OBJECT_BASIC_PROCESS_ID_LIST, ctypes.byref(info), ctypes.sizeof(info), None,
    ):
        return set()
    return {int(info.ProcessIdList[i]) for i in range(info.NumberOfProcessIdsInList)}


# ── Setup check ─────────────────────────────────────────────────────

PROBE_SCRIPT = r"""
import json, sqlite3, subprocess, sys
out = {}
try:
    out["whoami"] = subprocess.run(["whoami"], capture_output=True, text=True, timeout=15).stdout.strip()
except Exception:
    out["whoami"] = ""
try:
    import forven.bot_factory.runner  # noqa: F401
    out["imports_forven"] = True
except Exception as exc:
    out["imports_forven"] = repr(exc)
try:
    conn = sqlite3.connect(sys.argv[1], timeout=5)
    conn.execute("SELECT count(*) FROM bot_configs").fetchone()
    conn.close()
    out["opens_database"] = True
except Exception as exc:
    out["opens_database"] = repr(exc)
readable = []
for path in sys.argv[2:]:
    try:
        with open(path, "rb") as fh:
            fh.read(1)
        readable.append(path)
    except OSError:
        pass
out["readable_secrets"] = readable
print("FORVEN_PROBE " + json.dumps(out), flush=True)
"""


def judge_probe(output: str, account: BotOsAccount) -> dict:
    line = next((ln for ln in output.splitlines() if ln.startswith("FORVEN_PROBE ")), "")
    if not line:
        tail = output.strip()[-800:] or "(no output)"
        return {"ok": False, "problems": [f"The probe did not run as {account.username}: {tail}"]}
    result = json.loads(line[len("FORVEN_PROBE "):])
    problems = []
    whoami = str(result.get("whoami") or "").strip().lower()
    expected = account.domain_and_user[1].lower()
    if whoami.rsplit("\\", 1)[-1] != expected:
        problems.append(f"The probe ran as {whoami or 'an unknown user'}, not {account.username}")
    if result.get("imports_forven") is not True:
        problems.append(f"The bot account cannot load Forven's code: {result.get('imports_forven')}")
    if result.get("opens_database") is not True:
        problems.append(f"The bot account cannot open the database: {result.get('opens_database')}")
    for path in result.get("readable_secrets") or []:
        problems.append(f"The bot account can read {path}")
    return {"ok": not problems, "problems": problems, "probe": result}
