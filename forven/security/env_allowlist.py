"""Subprocess environment allowlist.

Hermes-inspired Phase 0: when Forven shells out via run_shell / run_code /
any other ``asyncio.create_subprocess_*`` or ``subprocess.run`` call, the
child process must NOT inherit the full parent environment. Inheriting
``OPENAI_API_KEY`` etc. into a tool subprocess is the most direct path
for a prompt-injected command to exfiltrate secrets via ``echo $OPENAI_API_KEY``.

Policy:
- **Allow** a fixed set of operationally-required vars by name (PATH,
  HOME, USER, LANG, locale + temp + Python runtime vars, FORVEN_HOME).
- **Block** anything matching secret-shaped name patterns regardless of
  whether it's otherwise on the allow list.
- Callers can pass `extra` for explicit additions — those bypass both
  allow and block filters since the caller has explicit knowledge.

Tirith-style command pattern scanning (rm -rf, fork bombs, pipe-to-shell)
is out of scope for Phase 0 and lands in the Phase 2 sandbox work.
"""

from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("forven.security.env_allowlist")

# Variable names allowed by exact match against this pattern. Case-sensitive
# on Unix; Windows callers should normalize beforehand if desired.
#
# PYTHONPATH / PYTHONHOME are deliberately NOT allowed (ENV-HERMETIC-1,
# 2026-07-06): on machines with a global Anaconda they re-root a child's
# dependency resolution away from the project venv — the registration sandbox
# then fails to import pandas even though the parent runs fine. PYTHONHOME
# re-roots the entire stdlib. A caller that genuinely needs a Python path
# passes it via `extra` (the sandbox worker pins PYTHONPATH to the repo root).
#
# The Windows service vars (APPDATA/LOCALAPPDATA/PROGRAMDATA/ALLUSERSPROFILE/
# HOMEDRIVE/HOMEPATH/WINDIR/SYSTEMDRIVE) ARE allowed: they carry no secrets,
# and native-extension DLL loading + user-profile resolution can fail without
# them — a failure mode that only reproduces inside the scrubbed child, never
# in the parent.
_ALLOW_NAME = re.compile(
    r"^(PATH|HOME|USER|USERNAME|USERPROFILE|LANG|LC_ALL|LC_[A-Z_]+|TERM|SHELL|"
    r"TMPDIR|TMP|TEMP|XDG_[A-Z_]+|"
    r"PYTHONIOENCODING|PYTHONUNBUFFERED|"
    r"FORVEN_HOME|FORVEN_PROFILE|"
    r"SYSTEMROOT|COMSPEC|PATHEXT|PROCESSOR_ARCHITECTURE|NUMBER_OF_PROCESSORS|"
    r"APPDATA|LOCALAPPDATA|PROGRAMDATA|ALLUSERSPROFILE|HOMEDRIVE|HOMEPATH|"
    r"WINDIR|SYSTEMDRIVE|"
    r"OS|COMPUTERNAME)$"
)

# Block any var whose NAME contains these substrings (case-insensitive),
# even if it would otherwise pass the allow list.
_BLOCK_NAME = re.compile(
    r"(?i)(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|PRIVATE)"
)


def build_subprocess_env(
    extra: dict[str, str] | None = None,
    *,
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a filtered env dict for spawning subprocesses.

    Args:
        extra: caller-provided explicit additions. NOT filtered — caller's
            responsibility (e.g. an MCP stdio adapter that needs to pass an
            API key through to a server it owns).
        base: env to filter (defaults to ``os.environ``). Override for
            testability.

    Returns:
        A dict suitable to pass as ``env=`` to subprocess / asyncio.create_subprocess_*.
        NEVER includes the parent's full env.
    """
    source = base if base is not None else dict(os.environ)
    out: dict[str, str] = {}
    dropped = 0

    for name, value in source.items():
        # Allow first.
        if not _ALLOW_NAME.match(name):
            dropped += 1
            continue
        # Block defense-in-depth — even allowed names get filtered if they
        # smell secret. Belt and suspenders.
        if _BLOCK_NAME.search(name):
            dropped += 1
            continue
        out[name] = value

    if extra:
        # Caller-explicit additions, not filtered.
        for name, value in extra.items():
            out[name] = value

    if dropped:
        log.debug("subprocess env allowlist dropped %d vars", dropped)

    return out


__all__ = ["build_subprocess_env"]
