"""AST-based static guard for AI-generated strategy source (P2-T02).

Single-pass walk over the parsed AST that records ALL violations without
bailing on the first. Never executes user code — uses `ast.parse` only.

Public API:
- :func:`scan_source` — scan an in-memory source string
- :func:`scan_file`   — read+scan a file from disk

Both return an :class:`AstReport`. `ok` is True iff no findings were
recorded. Findings cover: imports NOT on the allowlist (the import gate is an
allowlist of numeric/data libs + pure-computation stdlib + the strategy-facing
forven API — see :data:`ALLOWED_IMPORTS` / :data:`ALLOWED_FORVEN_PREFIXES`),
re-export smuggling (`from pandas.io.common import os`) and attribute traversal
to a dangerous module (`pd…subprocess.run`), dynamic execution constructs
(`eval`/`exec`/`compile`/`__import__('…')`/`getattr(__builtins__, …)`), and size
caps (file bytes + line count).

NOT a complete trust boundary by itself — a static scan can never be. It is
defense-in-depth that shrinks the surface; the real boundary is out-of-process
execution (Phase 2, see the 2026-06 security-hardening plan).
"""
from __future__ import annotations

import ast
import codecs
import io
import re
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# OHLCV column names a strategy legitimately indexes/getattrs (``df['open']``,
# ``getattr(row, 'close')``). They are carved OUT of the dynamic-access denylists
# below so the new subscript/getattr hardening never false-flags real strategies.
_OHLCV_NAMES: frozenset[str] = frozenset(
    {"open", "high", "low", "close", "volume", "vwap", "funding", "oi"}
)

# PEP 263 source-encoding cookie. Mirrors CPython's tokenizer regex
# (Lib/tokenize.py ``cookie_re``) so the guard rejects every cookie the
# interpreter would actually honor. The scan/compile differential (the guard
# parses a decoded ``str`` where ``ast.parse`` IGNORES the cookie, while
# importlib compiles the on-disk BYTES and HONORS it) was a confirmed in-process
# RCE — utf-7/unicode_escape re-decode a benign-looking comment into live code.
# Fix: refuse any source whose declared encoding is not a plain utf-8/ascii.
_CODING_COOKIE_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")
_SAFE_SOURCE_ENCODINGS: frozenset[str] = frozenset({"utf-8", "utf-8-sig", "ascii"})

# Hard caps. Configurable via forven.config.sandbox in a later phase.
MAX_FILE_BYTES: int = 100 * 1024  # 100 KB
MAX_LINES: int = 1500

# P1.2 (audit 2026-06-28): the import gate is an ALLOWLIST, not a denylist. A
# denylist of "dangerous" stdlib is unwinnable — `_winapi`, `http.client`,
# `pdb.run`, `timeit`, `winreg`, `xmlrpc`, `poplib`, ... each reach code-exec or
# network and a fresh one ships every Python release. A pure-OHLCV strategy only
# ever needs numeric/data libraries, pure-computation stdlib, and the
# strategy-facing forven API; everything else is rejected. (The prior denylist is
# preserved in git history.)
ALLOWED_IMPORTS: frozenset[str] = frozenset(
    {
        # Numeric / data / TA libraries the strategy corpus is built on.
        "pandas",
        "numpy",
        "pandas_ta",
        "scipy",
        "sklearn",
        # Pure-computation stdlib — none of these expose a filesystem, network,
        # process, or dynamic-exec primitive.
        "math",
        "statistics",
        "decimal",
        "fractions",
        "random",
        "json",
        "re",
        "string",
        "datetime",
        "typing",
        "dataclasses",
        "abc",
        "enum",
        "collections",
        "itertools",
        "functools",
        "warnings",
        "__future__",
    }
)

# The ONLY forven module prefixes an untrusted strategy may import: the strategy
# base, the pure-indicator facade, the composite/builtin strategies it may compose,
# the read-only market-data view, and the pure TA helpers. This EXCLUDES
# forven.exchange / forven.db / forven.secret_storage / forven.config / forven.auth /
# forven.brain — which expose orders, the database, and credentials.
#
# REMOVED for R3 (the 2026-06 strategy-import security audit):
#  • forven.scanner — re-exports get_db / kv_get / _execute_direct (DB + secret-decrypt
#    + live-order sink). Strategies only ever needed its PURE indicator helpers
#    (rsi/atr/adx/...), which are now re-exported by forven.strategies.indicators (a
#    lazy facade). The builtin/composite corpus was migrated to import from there; the
#    only files still importing forven.scanner are archived/dead customs (correctly
#    scan-rejected on revival). The worker DB+network jail already made scanner inert
#    inside the sandbox; de-allowlisting also removes it from the *intended* surface.
#  • forven.strategies.sentiment — re-exports a live funding fetch (network/exchange).
#    The one shipped user (builtin funding.py) was migrated to read the parent-enriched
#    funding_rate column; remaining importers are archived/dead.
#  • forven.data / forven.data_manager — ccxt client (SSRF), raw requests.Session
#    (exfil egress), and pathlib.Path/shutil (arbitrary FS read/write — NOT covered by
#    the DB+network jail). Only archived/dead customs imported them.
ALLOWED_FORVEN_PREFIXES: tuple[str, ...] = (
    "forven.strategies.base",
    "forven.strategies.indicators",
    "forven.strategies.composite",
    "forven.strategies.builtin",
    "forven.market_data_view",
    "forven.ta",
)

# Module names that must never be bound as an imported SYMBOL
# (`from pandas.io.common import os`) nor reached as an ATTRIBUTE
# (`pd._config.localization.subprocess.run`) — the two re-export smuggling routes
# back to os/subprocess through an otherwise-allowed package. Kept deliberately
# TIGHT so it can never collide with a legitimate submodule of an allowed library
# (numpy.random, scipy.signal, pandas.io, …): only names that are never such a
# submodule appear here.
_TRAVERSAL_BLOCK: frozenset[str] = frozenset(
    {
        "os",
        "subprocess",
        "socket",
        "sys",
        "builtins",
        "__builtin__",
        "__builtins__",
        "ctypes",
        "importlib",
        "_winapi",
        "_posixsubprocess",
        "posix",
        "nt",
        "msvcrt",
        "winreg",
        "pty",
        "multiprocessing",
        "popen",
        # Native-FFI / build / shell submodules that live INSIDE allowlisted libs
        # (numpy.ctypeslib -> ctypes.CDLL; numpy.distutils.exec_command -> shell;
        # numpy.f2py.compile -> native build). The import allowlist only checks the
        # top package, and these are reachable as plain attributes (``np.ctypeslib``)
        # — block both the attribute hop and the dotted import (see _DENY_SUBMODULES).
        "ctypeslib",
        "distutils",
        "f2py",
    }
)

# Dotted submodules of an ALLOWLISTED top package that are still forbidden: they
# wrap native-code loaders, shell helpers, or pickle/IO that an OHLCV strategy
# never needs. The import gate checks the top package only, so reject these by
# full dotted prefix as well (confirmed gadgets: numpy.distutils.exec_command,
# numpy.ctypeslib.load_library, numpy.f2py.compile).
_DENY_SUBMODULES: tuple[str, ...] = (
    "numpy.distutils",
    "numpy.f2py",
    "numpy.ctypeslib",
    "numpy.testing",
    "numpy.core",
    "scipy.io",
    "scipy.weave",
    "scipy.misc",
    "sklearn.externals",
    "pandas.io.clipboard",
)


def _module_import_allowed(dotted: str) -> bool:
    """Allowlist check for a (possibly dotted) module name."""
    name = (dotted or "").strip()
    if not name:
        return False
    if name.split(".")[0] == "forven":
        return any(name == p or name.startswith(p + ".") for p in ALLOWED_FORVEN_PREFIXES)
    if name.split(".")[0] not in ALLOWED_IMPORTS:
        return False
    # Top package is allowlisted, but a dangerous submodule is not (the gate must
    # validate the FULL dotted path, not just split('.')[0]).
    for bad in _DENY_SUBMODULES:
        if name == bad or name.startswith(bad + "."):
            return False
    return True


def _forbidden_import_message(dotted: str, *, from_import: bool = False) -> str:
    """Rejection message for a blocked import, self-documenting for forven paths.

    Agents guessing a nonexistent internal module (e.g. ``forven.strategies.signals``
    for a Signal enum that doesn't exist) burn registration attempts against a bare
    "not on the allowlist" — name the real strategy-facing surface instead.
    """
    shown = f"'from {dotted} import ...'" if from_import else f"'{dotted}'"
    message = f"Import not on the allowlist: {shown}"
    if (dotted or "").strip().split(".")[0] == "forven":
        message += (
            "; the only strategy-facing forven imports are "
            + ", ".join(ALLOWED_FORVEN_PREFIXES)
            + " (BaseStrategy and Signal live in forven.strategies.base)"
        )
    return message

FORBIDDEN_CALLS: frozenset[str] = frozenset(
    {
        "eval",
        "exec",
        "compile",
        # Filesystem / introspection builtins an honest trading strategy never
        # needs. `open` is the bare file primitive that lets generated code read
        # ~/.forven credentials/DB; globals/vars/locals expose module globals
        # (and thus __builtins__) used in sandbox-escape gadget chains.
        "open",
        "globals",
        "vars",
        "locals",
        "input",
        "breakpoint",
    }
)

# Dunder attributes that form the standard CPython sandbox-escape gadget chains
# (e.g. ``().__class__.__bases__[0].__subclasses__()`` or ``fn.__globals__``).
# A strategy that only computes indicators over OHLCV never touches these, so
# reaching for one is a strong signal of an escape attempt. The AST denylist is
# NOT a complete trust boundary (run untrusted strategies with the subprocess
# sandbox enabled) but closing these closes the obvious bypasses.
FORBIDDEN_ATTRS: frozenset[str] = frozenset(
    {
        "__subclasses__",
        "__bases__",
        "__base__",
        "__mro__",
        "__globals__",
        "__builtins__",
        "__getattribute__",
        "__subclasshook__",
        "__code__",
        "__closure__",
        "__import__",
        "__loader__",
        "__self__",
        # Frame / generator / coroutine / async-gen / traceback introspection.
        # A confirmed bypass reached the live builtins via
        # ``(x for x in [1]).gi_frame.f_builtins['exec']`` and read the trusted
        # PARENT frame's secrets via ``err.__traceback__.tb_frame.f_back.f_locals``.
        # None of these are touched by an honest indicator strategy.
        "gi_frame",
        "gi_code",
        "cr_frame",
        "cr_code",
        "ag_frame",
        "ag_code",
        "tb_frame",
        "tb_next",
        "f_back",
        "f_builtins",
        "f_globals",
        "f_locals",
        "f_code",
        "__traceback__",
        # Reflection dunders that reach class internals / re-create callables /
        # unwrap decorators / expose function defaults — all gadget-chain hops a
        # pure-computation strategy never needs. (``__dict__`` is intentionally NOT
        # blocked: real strategies read ``StrategyParams().__dict__`` for defaults,
        # and it is not load-bearing for any confirmed escape now that the frame
        # attrs + __globals__/__builtins__ hops are blocked.)
        "__reduce__",
        "__reduce_ex__",
        "__wrapped__",
        "__func__",
        "__defaults__",
        "__kwdefaults__",
    }
)

# Dangerous callables that an honest indicator strategy never needs. These are
# blocked in BOTH bare-name form (``eval(x)``) AND attribute form
# (``builtins.eval(x)``, ``b.open(...)``) — the attribute form was the verified
# bypass of the old denylist, which only checked ``ast.Name`` callees. Most of
# these are builtins reachable without an import, or live on modules the import
# allowlist already blocks, so the attribute check is defense-in-depth that closes
# the "reach the same primitive off a re-exported module" gadget.
FORBIDDEN_CALL_ATTRS: frozenset[str] = frozenset(
    {
        "eval",
        "exec",
        "execfile",
        "compile",
        "open",
        "__import__",
        "getattr",
        "setattr",
        "delattr",
        "vars",
        "globals",
        "locals",
        "memoryview",
        # os process-spawn family (os import is already blocked; belt-and-suspenders)
        "system",
        "popen",
        "fork",
        "forkpty",
        "execv",
        "execve",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execvp",
        "execvpe",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        # Process-spawn / file-launch / env-read names the old list missed —
        # reachable off a re-exported subprocess/os if the attribute traversal is
        # somehow bypassed (audit P1.2). `.run`/`.call` are intentionally NOT here
        # (too generic — a strategy may legitimately define a method named run);
        # the `.subprocess`/`.os` attribute block is what stops `subprocess.run`.
        "Popen",
        "check_output",
        "check_call",
        "startfile",
        "posix_spawn",
        "posix_spawnp",
        "getenv",
        "putenv",
        # Native-code loaders / FFI / build-shell gadgets reachable off allowlisted
        # numeric libs (numpy.ctypeslib.load_library, ctypes.CDLL via ctypeslib,
        # numpy.distutils.exec_command, numpy.f2py.compile). An honest strategy
        # never loads a shared library or shells out a compiler.
        "load_library",
        "LoadLibrary",
        "CDLL",
        "WinDLL",
        "OleDLL",
        "PyDLL",
        "cdll",
        "windll",
        "oledll",
        "dlopen",
        "exec_command",
    }
)

# Method names that read/deserialize files or URLs. A strategy operates on the
# OHLCV frame it is GIVEN — it never needs to read a file or fetch a URL, and
# several of these (read_pickle, read_hdf, the *_pickle/joblib loaders) execute
# arbitrary pickled code, while read_pickle/read_csv/read_* also accept http(s)
# URLs (server-side fetch with no SSRF guard). Blocked regardless of receiver.
FORBIDDEN_METHOD_NAMES: frozenset[str] = frozenset(
    {
        "read_pickle",
        "to_pickle",
        "read_hdf",
        "to_hdf",
        "read_parquet",
        "to_parquet",
        "read_feather",
        "to_feather",
        "read_orc",
        "read_sql",
        "read_sql_query",
        "read_sql_table",
        "to_sql",
        "read_gbq",
        "read_html",
        "read_xml",
        "read_stata",
        "read_sas",
        "read_spss",
        "read_csv",
        "read_table",
        "read_fwf",
        "read_excel",
        "read_clipboard",
        # numpy file primitives (np.load(allow_pickle=True) is special-cased below)
        "loadtxt",
        "genfromtxt",
        "fromfile",
        "memmap",
        "savetxt",
        "fromregex",
        # WRITE serializers the old denylist forgot. ndarray.tofile gives byte-exact
        # arbitrary file write; the pandas to_* text writers all accept a path. A
        # confirmed finding overwrote the never-scanned custom/__init__.py via
        # ``np.frombuffer(...).tofile(__file__...)`` -> persistent RCE on restart. A
        # strategy operates on the frame it is GIVEN; it never serializes to disk.
        "to_csv",
        "to_json",
        "to_html",
        "to_markdown",
        "to_latex",
        "to_xml",
        "to_excel",
        "save",
        "savez",
        "savez_compressed",
        "tofile",
    }
)

# Bare names that point at the builtins namespace from module scope without an
# import (``__builtins__['eval'](...)`` / ``__builtins__.eval(...)``).
FORBIDDEN_NAMES: frozenset[str] = frozenset({"__builtins__", "__builtin__"})

# Dangerous builtins that an honest indicator strategy never *names* — only ever
# (mis)used as a bare call. Referencing one as a value rather than calling it
# directly is the alias / indirection bypass the 2026-06-22 audit found: the old
# denylist only inspected ``Call.func``, so ``e = eval; e("...")`` (or passing
# ``eval`` into ``map``/``reduce``/a list) slipped through with zero findings and
# then executed in-process. We flag any *Load* of these names that is NOT the
# direct callee of a Call (direct calls are already judged by ``visit_Call``).
# Restricted to names that are never plausibly a local variable, so a normal
# strategy is never false-flagged. NOT a complete trust boundary — it closes the
# obvious one-line aliases, not every gadget (see module docstring).
FORBIDDEN_NAME_LOADS: frozenset[str] = frozenset(
    {
        "eval",
        "exec",
        "execfile",
        "compile",
        "__import__",
        "breakpoint",
        "open",
        "getattr",
        "setattr",
        "delattr",
    }
)

# String constants that must never be the key of a getattr/setattr/delattr/hasattr.
# The old check only blocked dunders in FORBIDDEN_ATTRS, so getattr(m, 'os') /
# getattr(m, 'subprocess') / getattr(m, 'system') / getattr(m, 'environ') all
# passed and reached the real module/builtin (confirmed bypass). Now reject every
# dangerous name, MINUS the OHLCV column names a strategy legitimately getattrs.
_FORBIDDEN_GETATTR_STR: frozenset[str] = frozenset(
    (
        FORBIDDEN_ATTRS
        | _TRAVERSAL_BLOCK
        | FORBIDDEN_CALL_ATTRS
        | FORBIDDEN_METHOD_NAMES
        | FORBIDDEN_NAMES
        | {"__builtins__", "environ", "modules", "builtins"}
    )
    - _OHLCV_NAMES
)

# String constants that are never a legitimate subscript key (``d['__globals__']``,
# ``builtins_dict['exec']``). Kept DELIBERATELY narrow — only dunder-form keys and
# the three pure-exec builtins — so it can never collide with an OHLCV/data column
# name like ``df['open']`` or ``df['close']``. Defense-in-depth behind the frame-attr
# and __builtins__-name blocks, which already kill the known chains.
_DANGEROUS_SUBSCRIPT_KEYS: frozenset[str] = frozenset({"exec", "eval", "compile"})

FindingKind = Literal[
    "forbidden_import",
    "dynamic_exec",
    "forbidden_encoding",
    "file_too_large",
    "too_many_lines",
    "syntax_error",
]


def _offending_source_encoding(source: str) -> str | None:
    """Return the declared NON-utf8/ascii source encoding (PEP 263 cookie), or None.

    CPython's tokenizer honors a ``# coding: <enc>`` cookie on line 1 or 2 (and a
    UTF-8 BOM); ``ast.parse`` on an already-decoded ``str`` does NOT. So a guard
    that scans the decoded str sees a benign comment while importlib re-decodes the
    file bytes under the cookie and runs different code. Honest strategy files are
    plain utf-8/ascii, so any other declared codec is rejected outright.
    """
    body = source[1:] if source[:1] == "\ufeff" else source
    for line in body.split("\n", 2)[:2]:
        match = _CODING_COOKIE_RE.match(line)
        if not match:
            continue
        declared = match.group(1)
        try:
            normalized = codecs.lookup(declared).name.replace("_", "-").lower()
        except Exception:
            return declared  # unknown codec — reject (the loader would too)
        if normalized not in _SAFE_SOURCE_ENCODINGS:
            return declared
    return None


@dataclass
class Finding:
    kind: FindingKind
    lineno: int
    col: int
    message: str
    node_repr: str


@dataclass
class AstReport:
    ok: bool
    findings: list[Finding] = field(default_factory=list)
    file_size_bytes: int = 0
    line_count: int = 0


class _GuardVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.findings: list[Finding] = []
        # id() of every ``ast.Name`` that is the direct callee of a Call. Such
        # names are judged by ``visit_Call`` (FORBIDDEN_CALLS etc.); the
        # alias check in ``visit_Name`` must skip them so legitimate direct
        # calls like ``getattr(o, "close")`` are not double-flagged/blocked.
        self._direct_call_funcs: set[int] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if not _module_import_allowed(alias.name):
                self.findings.append(
                    Finding(
                        kind="forbidden_import",
                        lineno=node.lineno,
                        col=node.col_offset,
                        message=_forbidden_import_message(alias.name),
                        node_repr=ast.dump(node),
                    )
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if getattr(node, "level", 0):
            # Relative import — an untrusted strategy module has no package to
            # reach into; reject so `from . import os` / `from .. import x` can't
            # smuggle a sibling module past the dotted-name allowlist.
            self.findings.append(
                Finding(
                    kind="forbidden_import",
                    lineno=node.lineno,
                    col=node.col_offset,
                    message="Relative imports are not allowed",
                    node_repr=ast.dump(node),
                )
            )
        elif not _module_import_allowed(module):
            self.findings.append(
                Finding(
                    kind="forbidden_import",
                    lineno=node.lineno,
                    col=node.col_offset,
                    message=_forbidden_import_message(module, from_import=True),
                    node_repr=ast.dump(node),
                )
            )
        # Smuggle guard: even from an allowed package, refuse to BIND a dangerous
        # module as a symbol — `from pandas.io.common import os` has an allowed
        # top package (pandas) but binds the real os module.
        for alias in node.names:
            if alias.name.split(".")[0] in _TRAVERSAL_BLOCK:
                self.findings.append(
                    Finding(
                        kind="forbidden_import",
                        lineno=node.lineno,
                        col=node.col_offset,
                        message=f"Forbidden imported symbol: '{alias.name}'",
                        node_repr=ast.dump(node),
                    )
                )
        self.generic_visit(node)

    def _add(self, node: ast.AST, message: str) -> None:
        self.findings.append(
            Finding(
                kind="dynamic_exec",
                lineno=getattr(node, "lineno", 0),
                col=getattr(node, "col_offset", 0),
                message=message,
                node_repr=ast.dump(node),
            )
        )

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in FORBIDDEN_NAMES:
            self._add(node, f"Forbidden name: '{node.id}' (builtins namespace access)")
        elif (
            isinstance(node.ctx, ast.Load)
            and node.id in FORBIDDEN_NAME_LOADS
            and id(node) not in self._direct_call_funcs
        ):
            self._add(
                node,
                f"Forbidden reference to dangerous builtin '{node.id}' "
                "(alias/indirection of a blocked call)",
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        # Record the direct callee Name BEFORE descending into children so the
        # alias check in visit_Name can exempt it (direct calls are vetted here).
        if isinstance(func, ast.Name):
            self._direct_call_funcs.add(id(func))

        # Attribute-form dangerous calls — the verified bypass of the old
        # bare-name-only check (e.g. ``builtins.eval(...)``, ``b.open(...)``,
        # ``pd.read_pickle(url)``, ``np.load(p, allow_pickle=True)``).
        if isinstance(func, ast.Attribute):
            if func.attr in FORBIDDEN_CALL_ATTRS:
                self._add(node, f"Forbidden call: '.{func.attr}(...)'")
            elif func.attr in FORBIDDEN_METHOD_NAMES:
                self._add(node, f"Forbidden file/deserialization method: '.{func.attr}(...)'")
            elif func.attr in {"load", "loads"}:
                # numpy.load / *.load(...) is only dangerous with allow_pickle truthy.
                for kw in node.keywords:
                    if kw.arg == "allow_pickle" and not (
                        isinstance(kw.value, ast.Constant) and kw.value.value in (False, 0, None)
                    ):
                        self._add(node, "Forbidden call: '.load(..., allow_pickle=...)' (pickle deserialization)")
                        break
            elif func.attr == "query":
                # pandas DataFrame.query routes an opaque attacker string through an
                # evaluator. The 'python' engine allows attribute reads + method calls — a
                # confirmed RCE gadget (df.query("close.__init__.__globals__[...]...")) — and
                # when numexpr is not installed pandas SILENTLY DEFAULTS to the python
                # engine, so an ABSENT or NON-CONSTANT engine is just as dangerous as an
                # explicit engine="python". Allow ONLY a constant engine="numexpr" (numeric-
                # only, no attribute access); reject every other form. (.eval is already a
                # FORBIDDEN_CALL_ATTR.)
                safe_numexpr = any(
                    kw.arg == "engine"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value == "numexpr"
                    for kw in node.keywords
                )
                if not safe_numexpr:
                    self._add(
                        node,
                        "Forbidden call: '.query(...)' without a constant engine=\"numexpr\" "
                        "(the python engine — used by default when numexpr is absent — evals attacker strings)",
                    )

        # Bare getattr/setattr/delattr with a NON-constant attribute key is the
        # dynamic-attribute escape primitive (e.g. getattr(b, 'ev'+'al')). The
        # constant-dunder form is handled further below.
        if (
            isinstance(func, ast.Name)
            and func.id in {"getattr", "setattr", "delattr"}
            and len(node.args) >= 2
            and not (
                isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
            )
        ):
            self._add(node, f"Forbidden dynamic attribute access: '{func.id}(..., <non-constant>)'")

        if isinstance(func, ast.Name) and func.id in FORBIDDEN_CALLS:
            self.findings.append(
                Finding(
                    kind="dynamic_exec",
                    lineno=node.lineno,
                    col=node.col_offset,
                    message=f"Forbidden call: '{func.id}(...)'",
                    node_repr=ast.dump(node),
                )
            )
        elif isinstance(func, ast.Name) and func.id == "__import__":
            # A dynamic import is safe ONLY when its argument is a CONSTANT string
            # naming an ALLOWLISTED module — that is exactly equivalent to a plain
            # `import <name>` and is a common codegen idiom (`__import__("pandas")`).
            # A non-constant argument (the real obfuscation/exfil primitive) or a
            # non-allowlisted module (os/socket/ctypes/…) stays blocked.
            const_mod: str | None = None
            if (
                node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                const_mod = node.args[0].value
            if const_mod is None or not _module_import_allowed(const_mod):
                self.findings.append(
                    Finding(
                        kind="dynamic_exec",
                        lineno=node.lineno,
                        col=node.col_offset,
                        message="Forbidden dynamic import: '__import__(...)'",
                        node_repr=ast.dump(node),
                    )
                )
        elif (
            isinstance(func, ast.Name)
            and func.id == "getattr"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "__builtins__"
        ):
            self.findings.append(
                Finding(
                    kind="dynamic_exec",
                    lineno=node.lineno,
                    col=node.col_offset,
                    message="Forbidden dynamic access: 'getattr(__builtins__, ...)'",
                    node_repr=ast.dump(node),
                )
            )

        # getattr/setattr/delattr/hasattr with a dunder string constant is the
        # string-form of the attribute-traversal escape (e.g.
        # ``getattr(obj, "__globals__")``); block it alongside the dotted form.
        if isinstance(func, ast.Name) and func.id in {
            "getattr",
            "setattr",
            "delattr",
            "hasattr",
        }:
            for _arg in node.args:
                if (
                    isinstance(_arg, ast.Constant)
                    and isinstance(_arg.value, str)
                    and _arg.value in _FORBIDDEN_GETATTR_STR
                ):
                    self.findings.append(
                        Finding(
                            kind="dynamic_exec",
                            lineno=node.lineno,
                            col=node.col_offset,
                            message=(
                                f"Forbidden dynamic attribute access: "
                                f"'{func.id}(..., {_arg.value!r})'"
                            ),
                            node_repr=ast.dump(node),
                        )
                    )
                    break

        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in FORBIDDEN_ATTRS:
            self.findings.append(
                Finding(
                    kind="dynamic_exec",
                    lineno=node.lineno,
                    col=node.col_offset,
                    message=f"Forbidden attribute access: '.{node.attr}'",
                    node_repr=ast.dump(node),
                )
            )
        elif node.attr in _TRAVERSAL_BLOCK:
            # Attribute-chain to a dangerous module re-exported off an allowed
            # package, e.g. `pandas._config.localization.subprocess` — the `.subprocess`
            # hop is caught here before any `.run(...)` can fire (audit P1.2).
            self.findings.append(
                Finding(
                    kind="dynamic_exec",
                    lineno=node.lineno,
                    col=node.col_offset,
                    message=f"Forbidden module attribute access: '.{node.attr}'",
                    node_repr=ast.dump(node),
                )
            )
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        # A constant dunder/exec-name subscript key is builtins-dict access reached
        # without a blocked Name/Attribute (``some_dict['exec'](...)``). Narrowly
        # scoped (dunder-form or exec/eval/compile) so it never collides with an
        # OHLCV column key like ``df['open']``. Defense-in-depth behind the
        # f_builtins / __builtins__ blocks.
        key = node.slice
        if (
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and (
                (key.value.startswith("__") and key.value.endswith("__"))
                or key.value in _DANGEROUS_SUBSCRIPT_KEYS
            )
        ):
            self._add(node, f"Forbidden subscript key: [{key.value!r}] (builtins/dunder access)")
        self.generic_visit(node)


def scan_source(source: str, file_size_bytes: int = 0) -> AstReport:
    """Scan a Python source string. *file_size_bytes* is reported as-given;
    when zero, it's filled in from `len(source.encode('utf-8'))`."""
    if file_size_bytes == 0 and source:
        file_size_bytes = len(source.encode("utf-8"))

    line_count = 0 if not source else source.count("\n") + (
        0 if source.endswith("\n") else 1
    )

    findings: list[Finding] = []

    # PEP 263 coding-cookie / BOM smuggling: the interpreter compiles the file
    # BYTES under the declared codec, but this guard parses the decoded str (which
    # ignores the cookie). Refuse any non-utf8/ascii source encoding so the program
    # we scan is the program that runs (confirmed utf-7/unicode_escape RCE).
    offending_enc = _offending_source_encoding(source)
    if offending_enc is not None:
        findings.append(
            Finding(
                kind="forbidden_encoding",
                lineno=1,
                col=0,
                message=(
                    f"Forbidden source-encoding declaration: '{offending_enc}'. "
                    "Only utf-8/ascii is allowed (a coding cookie lets the "
                    "interpreter compile different bytes than were scanned)."
                ),
                node_repr="",
            )
        )

    if file_size_bytes > MAX_FILE_BYTES:
        findings.append(
            Finding(
                kind="file_too_large",
                lineno=0,
                col=0,
                message=(
                    f"File is {file_size_bytes} bytes, exceeds "
                    f"the {MAX_FILE_BYTES}-byte limit"
                ),
                node_repr="",
            )
        )

    if line_count > MAX_LINES:
        findings.append(
            Finding(
                kind="too_many_lines",
                lineno=0,
                col=0,
                message=(
                    f"Source has {line_count} lines, exceeds "
                    f"the {MAX_LINES}-line limit"
                ),
                node_repr="",
            )
        )

    if not source:
        return AstReport(
            ok=len(findings) == 0,
            findings=findings,
            file_size_bytes=file_size_bytes,
            line_count=line_count,
        )

    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        findings.append(
            Finding(
                kind="syntax_error",
                lineno=exc.lineno or 0,
                col=exc.offset or 0,
                message=f"SyntaxError: {exc.msg}",
                node_repr="",
            )
        )
        return AstReport(
            ok=False,
            findings=findings,
            file_size_bytes=file_size_bytes,
            line_count=line_count,
        )

    visitor = _GuardVisitor()
    visitor.visit(tree)
    findings.extend(visitor.findings)

    return AstReport(
        ok=len(findings) == 0,
        findings=findings,
        file_size_bytes=file_size_bytes,
        line_count=line_count,
    )


def scan_file(path: Path | str) -> AstReport:
    """Read *path* and scan EXACTLY the bytes the interpreter would compile.

    Detect the source encoding from the raw bytes the same way CPython's loader
    does (``tokenize.detect_encoding`` honors the PEP 263 cookie + BOM), reject any
    non-utf8/ascii encoding, then decode under the detected codec so the scanned
    program equals the compiled program. No silent latin-1 fallback — an
    undecodable file is rejected, not reinterpreted."""
    p = Path(path)
    raw = p.read_bytes()
    try:
        detected, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    except SyntaxError as exc:
        return AstReport(
            ok=False,
            findings=[Finding("forbidden_encoding", 1, 0, f"Undecodable source: {exc}", "")],
            file_size_bytes=len(raw),
        )
    normalized = detected.replace("_", "-").lower()
    if normalized not in _SAFE_SOURCE_ENCODINGS:
        return AstReport(
            ok=False,
            findings=[
                Finding(
                    "forbidden_encoding",
                    1,
                    0,
                    f"Forbidden source encoding '{detected}' — only utf-8/ascii allowed.",
                    "",
                )
            ],
            file_size_bytes=len(raw),
        )
    try:
        source = raw.decode(detected)
    except UnicodeDecodeError as exc:
        return AstReport(
            ok=False,
            findings=[Finding("forbidden_encoding", 1, 0, f"Undecodable source: {exc}", "")],
            file_size_bytes=len(raw),
        )
    return scan_source(source, file_size_bytes=len(raw))
