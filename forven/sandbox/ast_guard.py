"""AST-based static guard for AI-generated strategy source (P2-T02).

Single-pass walk over the parsed AST that records ALL violations without
bailing on the first. Never executes user code — uses `ast.parse` only.

Public API:
- :func:`scan_source` — scan an in-memory source string
- :func:`scan_file`   — read+scan a file from disk

Both return an :class:`AstReport`. `ok` is True iff no findings were
recorded. Forbidden categories: top-level imports of OS/network/file/
subprocess/dynamic-exec stdlib modules, dynamic execution constructs
(`eval`/`exec`/`compile`/`__import__('…')`/`getattr(__builtins__, …)`),
and size caps (file bytes + line count).
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# Hard caps. Configurable via forven.config.sandbox in a later phase.
MAX_FILE_BYTES: int = 100 * 1024  # 100 KB
MAX_LINES: int = 1500

FORBIDDEN_IMPORTS: frozenset[str] = frozenset(
    {
        "os",
        "subprocess",
        "socket",
        "requests",
        "httpx",
        "aiohttp",
        "urllib",
        "urllib2",
        "urllib3",
        "ftplib",
        "smtplib",
        "telnetlib",
        "ctypes",
        "cffi",
        "multiprocessing",
        "threading",
        "asyncio",
        "concurrent",
        "pathlib",
        "shutil",
        "tempfile",
        "glob",
        "fileinput",
        "pickle",
        "marshal",
        "shelve",
        "dbm",
        "sqlite3",
        "zipfile",
        "tarfile",
        "importlib",
        "imp",
        "pkgutil",
        "pkg_resources",
        # Gadget-source stdlib modules: each gives a generated strategy a route
        # back to the builtins namespace / live objects / dynamic execution even
        # though it imports cleanly. A pure-OHLCV strategy never needs any of them,
        # so reaching for one is a strong escape signal (P-S audit 2026-06-22).
        "builtins",
        "__builtin__",
        "sys",
        "gc",
        "inspect",
        "io",
        "codecs",
        "code",
        "codeop",
        "runpy",
        "pty",
        "posix",
        "nt",
        "signal",
        "mmap",
        "fcntl",
        "resource",
        "webbrowser",
        "platform",
        "sysconfig",
        "site",
        "ast",
        "dis",
        # operator.attrgetter('__globals__')(fn) / operator.methodcaller reach the
        # gadget chain and dynamic dispatch without a dotted attribute the guard
        # can see. A pure-OHLCV strategy uses Python's native operators, never the
        # `operator` module, so block it (P-S audit 2026-06-22).
        "operator",
        # Third-party (de)serializers that execute pickled callables on load.
        "joblib",
        "dill",
        "cloudpickle",
        "yaml",
        # The `ta` technical-analysis library is permanently blocked at runtime by
        # the import tripwire in forven/__init__.py. Reject it here too so a
        # generated strategy that imports it fails fast during validation (and the
        # codegen retry can fix it) instead of crashing mid-backtest.
        "ta",
    }
)

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
    }
)

# Dangerous callables that an honest indicator strategy never needs. These are
# blocked in BOTH bare-name form (``eval(x)``) AND attribute form
# (``builtins.eval(x)``, ``b.open(...)``) — the attribute form was the verified
# bypass of the old denylist, which only checked ``ast.Name`` callees. Most of
# these are builtins reachable without an import, or live on modules already in
# FORBIDDEN_IMPORTS, so the attribute check is defense-in-depth that closes the
# "import a not-yet-banned module and reach the same primitive" gadget.
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

FindingKind = Literal[
    "forbidden_import",
    "dynamic_exec",
    "file_too_large",
    "too_many_lines",
    "syntax_error",
]


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
            top = alias.name.split(".")[0]
            if top in FORBIDDEN_IMPORTS:
                self.findings.append(
                    Finding(
                        kind="forbidden_import",
                        lineno=node.lineno,
                        col=node.col_offset,
                        message=f"Forbidden import: '{alias.name}'",
                        node_repr=ast.dump(node),
                    )
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        top = module.split(".")[0]
        if top in FORBIDDEN_IMPORTS:
            self.findings.append(
                Finding(
                    kind="forbidden_import",
                    lineno=node.lineno,
                    col=node.col_offset,
                    message=f"Forbidden import: 'from {module} import ...'",
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
            # naming a non-forbidden module — that is exactly equivalent to a plain
            # `import <name>` and is a common codegen idiom (`__import__("pandas")`).
            # A non-constant argument (the real obfuscation/exfil primitive) or a
            # forbidden module (os/socket/ctypes/…) stays blocked.
            const_mod: str | None = None
            if (
                node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                const_mod = node.args[0].value.split(".")[0]
            if const_mod is None or const_mod in FORBIDDEN_IMPORTS:
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
                    and (_arg.value in FORBIDDEN_ATTRS or _arg.value == "__builtins__")
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
    """Read *path* and scan it. UTF-8 with latin-1 fallback so we never
    raise UnicodeDecodeError out of the guard."""
    p = Path(path)
    raw = p.read_bytes()
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError:
        source = raw.decode("latin-1")
    return scan_source(source, file_size_bytes=len(raw))
