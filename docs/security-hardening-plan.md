# Security Hardening Plan

**Created:** 2026-06-28
**Status:** **Phase 1 IMPLEMENTED & TESTED (2026-06-28)** — Phases 2–4 pending. Two decisions still needed before Phase 2 (see *Decisions needed*).
**Source:** Full security audit (six-dimension parallel review + first-party verification of the critical findings)

> **Phase 1 landed (183 tests green):** P1.3 CSRF/Origin guard + `/api/shutdown` backstop · P1.4 Hyperliquid agent-wallet-only enforcement for live (`FORVEN_HL_ALLOW_MASTER_KEY` opt-out) · P1.1 `register_strategy`→codegen + new `develop` tool-context (denies catastrophic) · P1.2 AST guard denylist→**allowlist** (corpus re-scan: 4831/4932 pass, zero working strategies newly broken) · P1.5 Discord token encrypted at rest + redaction field-name/0x-normalize fixes · P1.6 exposed-bind now requires the operator key. **Phase 2 (out-of-process execution) is still the real RCE boundary.**

---

## Threat model & assumptions

Current deployment is **localhost, single-user desktop** (Tauri shell + local FastAPI + in-process trading daemon). Auth is fail-open on loopback by default. Given that, the dominant real-world threats — in priority order — are:

1. **Autonomous-agent RCE via untrusted-content ingestion.** Forven's agents ingest attacker-controllable web/Reddit/GitHub content and can author + register strategy code that executes **in-process with the live exchange credential reachable**. This is a complete, remotely-triggerable host-compromise chain. *This is the reason this plan exists.*
2. **Drive-by CSRF** from a malicious web page the operator visits, hitting the local API (which is reachable at `127.0.0.1` from the browser).
3. **Credential theft / lateral movement** if the host or the agent loop is compromised.

> **Scope flag:** If forven is ever served **networked or multi-user**, three currently-MEDIUM items (CORS/CSRF, operator-key-on-exposed-bind, operator-key-in-client-bundle) escalate to **CRITICAL**. Decide this early (see *Decisions needed*) because it re-ranks Phase 3.

---

## Priority summary

| ID | Item | Sev | Phase | Effort |
|----|------|-----|-------|--------|
| P1.1 | Break the autonomous inject→register trigger (context default-deny + approval gate) | CRIT chain | 1 | M |
| P1.2 | AST guard: denylist → **allowlist**; close `from`-import + `.run/.Popen` bypasses | CRIT surface | 1 | M |
| P1.3 | Drive-by/CSRF: `Origin` check on `/api/shutdown` + global CSRF guard | HIGH | 1 | S |
| P1.4 | Hyperliquid **agent-wallet-only** enforcement for live trading | HIGH | 1 | S–M |
| P1.5 | Secret hygiene: encrypt Discord token, close log-redaction gaps, normalize HL key | MED | 1 | S |
| P1.6 | Exposed-bind requires **operator** key, not just API key | MED | 1 | S |
| P2.1 | **Out-of-process strategy execution** (the real trust boundary) | CRIT fix | 2 | L |
| P3.1 | Path-traversal confinement in `parquet_path` + symbol charset | MED | 3 | S |
| P3.2 | Self-update integrity: signed/pinned commit, HTTPS-only remote | MED | 3 | M |
| P3.3 | Get the operator key **off the client**; `safeHref(returnTo)`; tighten `img-src` | MED | 3 | M |
| P3.4 | Research-fetch egress allowlist; document LLM-vendor data flow | MED | 3 | M |
| P3.5 | Data-at-rest: restrict DB file perms; drop pickle fallback; sim identifier validation | LOW | 3 | S |
| P3.6 | Dependency pinning / hash-locked install for preflight | LOW | 3 | S |
| P4.x | Regression corpus + CSRF/secret tests + CI security gate | — | 4 | M |

Effort: **S** ≈ ½–1 day · **M** ≈ 1–3 days · **L** ≈ 1–2 weeks.

**Sequencing rationale:** Phase 1 *stops the bleeding* — it breaks the remote RCE trigger and shrinks the exploit surface with small, low-risk diffs while the architectural fix is built. Phase 2 installs the *real boundary* that makes the AST guard non-load-bearing. Phase 3 mops up the mediums/lows. Phase 4 proves it and keeps it fixed.

---

## Phase 0 — Operator action (today, zero code)

- **Confirm the configured Hyperliquid key is an API/agent wallet, not a master-account key.** Agent wallets are protocol-barred from withdrawal/transfer, so even a full RCE cannot move funds off the platform — it caps the worst case to unauthorized trading. P1.4 will make this an enforced invariant; until then, verify it by hand.

---

## Phase 1 — Immediate hardening

### P1.1 — Break the autonomous inject → register trigger
**Why:** The RCE chain is only *remotely* dangerous because an autonomous agent can ingest hostile content and reach `register_strategy` with **no human in the loop**.
**Where:**
- `forven/agents/tool_registry.py:551-553` — `register_strategy` is *excluded* from the `codegen` category, so the research-context default-deny never covers it.
- `forven/agents/runner.py:1160` — tools-context is set only for `task_type == "research"`; `develop_candidate` and friends run with **no** category gate.
- `forven/control_plane/smart_approval.py` — only escalates `live_trade`/`real_money`; never sees registration.

**Fix:**
1. Add `register_strategy` (and any other code-authoring/registration tool) to the `codegen` category so the existing dispatch-time deny applies.
2. Make tools-context **default-deny**: every task type that can ingest external/cached content (`develop_candidate`, `code_strategy`, follow-through, …) gets a deny context, not just `research`. Flip the model from opt-in-restrict to opt-out-allow.
3. Add an **approval gate**: when an agent task that has ingested external content attempts `register_strategy`, route it through `smart_approval` and require operator confirmation. Operator-initiated registration is unaffected.

**Effort:** M · **Verify:** unit test that a `develop_candidate` agent cannot reach `register_strategy` without approval; that an external-content-ingesting task is default-deny at dispatch.

### P1.2 — AST guard: denylist → allowlist
**Why:** The guard is the *only* code-safety check on the in-process import path, and it is a leaky denylist (its own docstring says "NOT a complete trust boundary"). Verified bypasses: `import pandas; pandas._config.localization.subprocess.run([...])` and `from pandas.io.common import os`.
**Where:** `forven/sandbox/ast_guard.py` — `FORBIDDEN_IMPORTS:27-106`, `visit_ImportFrom:316-329`, `FORBIDDEN_CALL_ATTRS:157-194`, `visit_Attribute:475-486`.

**Fix:**
1. Replace `FORBIDDEN_IMPORTS` with an **import allowlist** — e.g. `pandas`, `numpy`, `math`, `statistics`, `datetime`, `decimal`, and the strategy base. Reject every other top-level module **and** every dotted submodule.
2. `visit_ImportFrom`: reject when any imported *name* resolves to a non-allowlisted/forbidden module (closes `from pandas.io.common import os`), and apply the allowlist to the full dotted path, not just `split(".")[0]`.
3. Add an **attribute allowlist** for calls (or, as a stopgap, add `run`, `Popen`, `call`, `check_output`, `startfile`, `posix_spawn`, `getenv` to the call-attr denylist). Note this is whack-a-mole; the allowlist is the real fix and Phase 2 is the actual boundary.

**Risk:** stricter scanning may reject legitimate generated strategies that import an off-allowlist module — the codegen retry loop must surface the rejection reason and let the model fix it. Validate against the full `builtin/` corpus so first-party strategies still pass.
**Effort:** M · **Verify:** the confirmed escape payloads become regression tests (must be rejected); every `forven/strategies/builtin/*.py` still passes `scan_source`.

### P1.3 — Stop drive-by control (CSRF / Origin)
**Why:** A malicious page the operator visits can POST to `127.0.0.1` and the request is delivered (CORS withholds only the *response*, not the request). Confirmed for the auth-exempt `/api/shutdown` (kills the trading daemon) and the keyless-default state-changers.
**Where:** `forven/api.py:673-677` (shutdown loopback-only check), CORS at `api.py:607-613`, sinks in `routers/ops.py`, `routers/updates.py:37`, `routers/paper.py`.

**Fix:**
1. `/api/shutdown`: reject when an `Origin`/`Referer` header is present and not in the localhost allowlist (keeps the headerless local-launcher call working).
2. Add a **global CSRF middleware**: for unsafe methods (POST/PUT/DELETE/PATCH), if `Origin` is present and not in the CORS allowlist → `403`. Equivalent to requiring a custom header that forces a (blocked) preflight.

**Effort:** S · **Verify:** cross-origin simple POST to `/api/shutdown` and `/api/system/stop` is rejected; same-origin and no-Origin requests still succeed.

### P1.4 — Hyperliquid agent-wallet-only enforcement
**Why:** Forven accepts a raw private key and can't tell an agent key from a master key cryptographically. "API keys can't withdraw" only holds if the pasted key is actually an agent key. Make it an enforced invariant.
**Where:** `forven/exchange/hyperliquid.py` — `_is_agent_trading_on_behalf:332`, `_is_agent_authorized:355`, the live-trade path in `_exchange_for_trading`.

**Fix:** before any **live** order, assert the key is a verified agent (separate `main_wallet`, key-derived address ≠ `main_wallet`, `_is_agent_authorized` True). Refuse live trading with a master key and explain why in Settings/onboarding. Paper/backtest unaffected.
**Effort:** S–M · **Verify:** a master key blocks live trading with a clear message; a properly-approved agent key is allowed.

### P1.5 — Secret hygiene quick wins
**Where / Fix:**
- **Discord token plaintext** (`api_core.py:2596-2603`, `config.py:save_config`): route `discord_token` through `encrypt_secret` like the webhook two lines below, and `_restrict_to_owner(CONFIG_FILE)`.
- **Log-redaction gaps** (`redact.py:63-66, 95-100`): add `private_key`, `access`, `refresh`, `token`, `id_token`, `api_secret` to the JSON-value alternation; normalize the HL key to `0x` at the storage boundary (`hyperliquid.py:906-909`) so the existing `0x`-anchored pattern always matches a bare-pasted key.

**Effort:** S · **Verify:** Discord token is ciphertext at rest; redactor unit tests cover the new field names and a bare key.

### P1.6 — Exposed-bind requires operator key
**Why:** On a non-loopback bind the guard checks only `FORVEN_API_KEY`, so the two-tier model collapses — one API key grants full operator control (incl. MCP stdio command exec).
**Where:** `forven/api_security.py:115-132` (`assert_safe_bind_host`), `:177-186` (`require_operator_access`).
**Fix:** when bind is non-loopback, also require a distinct `FORVEN_OPERATOR_KEY`; make `require_operator_access` fail-closed once any auth is enabled.
**Effort:** S · **Verify:** `0.0.0.0` bind without an operator key refuses to start.

---

## Phase 2 — The real boundary

> **Status 2026-06-28 (in progress):** the isolation **primitive** is built and proven —
> `forven/sandbox/strategy_worker.py` builds a strategy and runs its `generate_signals`
> in a subprocess (secret-free env via `build_subprocess_env`, network denied, FS
> confined, Win32 Job Object / POSIX rlimit caps, **parquet I/O — never pickle**, schema
> re-validated on return). The untrusted strategy module is imported **inside the worker**
> (AST guard runs there), never the trusted parent. A first wiring attempt was reverted
> after discovering the real boundary is the *strategy object's* `generate_signals` /
> `generate_signal` (built from the registry), NOT the builtin `_vectorized_directional_signals`
> path. **(1) DONE — persistent worker:** `strategy_worker.py` now has a `--serve` mode that
> imports + `discover()`s ONCE then loops on stdin requests; a reusable client (reader thread +
> queue + respawn-on-death/timeout) keeps the `compute_directional_signals_isolated` API and
> amortizes the ~15s startup across calls (parity + reuse tests green). **(2) DONE — backtest
> wiring (flag-gated):** `run_strategy_execution` routes a CUSTOM strategy's vectorized
> `generate_signals` through the worker when `FORVEN_ISOLATED_STRATEGY_EXEC` is on (OFF by
> default; builtins/composites untouched). A full off-vs-on backtest yields byte-identical
> kernel trades (`test_isolated_backtest_matches_in_process`). **(3) DONE — per-bar path +
> scanner:** the per-bar adapter (`_signals_from_per_bar`, walking `generate_signal`) now runs in
> a `per_bar` worker mode (`compute_per_bar_signals_isolated`), and `run_strategy_execution`'s
> per-bar fallback is routed through it (flag-gated, custom-only) — byte-identical
> (`test_isolated_per_bar_matches_in_process`). Because `run_strategy_execution` is the SHARED
> backtest+scanner pipeline (`scanner.py:5973`), the live/paper **scanner** is covered by (2)+(3)
> with no scanner.py change. **Remaining:** (4) FULL isolation still needs
> the parent to STOP IMPORTING custom code — today `discover()` imports every custom in-process
> (AST-gated, once at startup) and `strategy_obj` is built in-process, so what's isolated is the
> heavy, repeated signal EXECUTION (vectorized + per-bar), not the one-time import / `__init__`. Closing
> that is the registry-loading refactor (large) — track separately.
>
> **Key design constraint discovered:** a strategy run in the worker sees ONLY the input
> frame — no DB, no network. So **all data a strategy needs must be enriched onto the df by
> the (trusted) parent before delegating** (funding/OI/cross-asset columns). Strategies that
> fetch their own data inside `generate_signals` behave differently in isolation — which is
> exactly the containment we want, but the parent must pre-supply legitimate data. The parity
> test therefore validates against *pure* OHLCV-only strategies.

### P2.1 — Out-of-process strategy execution
**Why:** Phase 1 shrinks and gates the RCE surface but the AST guard is still load-bearing. The durable fix is to make **process isolation** the trust boundary so a guard bypass is contained, not catastrophic. Forven already built the isolation primitive (`build_subprocess_env`, `sandbox/run_code`) — it's just not on the execution path; only one-shot validation uses it.
**Where (the in-process execution sites to move):** `forven/strategies/registry.py:474-483`, `forven/strategies/backtest.py:2019`, `forven/strategies/optimizer.py:1033`, the scanner's per-bar signal eval, `forven/api_core.py:10117-10125`.

**Design:**
- Isolate the *untrusted* unit — the strategy's signal generation (`generate_signal` / the vectorized signal function) — in a worker process. The **execution kernel itself is first-party trusted code** operating on data, so it stays in-process: the worker emits the `DirectionalSignals` payload, the trusted kernel simulates. This keeps the blast-radius reduction without moving the whole backtest.
- Worker hardening: launched with `build_subprocess_env` (**no secrets in env**), FS confined to a throwaway temp dir (not `cwd=REPO_ROOT`), **network egress denied**.
- **Windows note (be honest):** true per-process network blocking is harder on Windows than Linux. Pragmatic layered approach: (a) scrubbed env means there are no secrets to exfiltrate anyway, (b) confine FS, (c) monkeypatch `socket`/`_socket` in the worker bootstrap to refuse non-loopback connects, (d) optionally run under a restricted Job Object / AppContainer token. The env-scrub + FS-confine + socket-deny combination is the baseline; the OS-token sandbox is a stretch goal.
- **Perf:** avoid per-tick spawn cost with a persistent worker pool and the existing scan-verdict/signal cache. Measure against the current scanner cadence.

**Decision point (see below):** pair this with restricting **agent-authored** strategies to the `rule_engine` spec (no free Python), allowing free Python only via operator-approved authoring.
**Effort:** L · **Verify:** the confirmed escape payloads — even if they slip past the guard — read an empty env, cannot open the creds DB, and cannot connect out; **parity preserved** (worker-produced signals == in-process signals across the builtin corpus and `test_execution_parity`).

---

## Phase 3 — Defense-in-depth (mediums/lows)

- **P3.1 Path traversal** (`forven/data.py:319-321`, `routers/data.py:303`): in `parquet_path()`, reject `symbol`/`timeframe` containing `/ \ .. :` or leading dots, then `resolve()` and assert `is_relative_to(DATA_DIR)`; enforce a strict symbol charset (`^[A-Z0-9][A-Z0-9-]{0,31}$`). Closes the CSV-upload write-anywhere and dataset-read primitives. **S**
- **P3.2 Self-update integrity** (`forven/self_update.py:239-285`): verify a signed tag/commit against a pinned key before fast-forward; refuse non-HTTPS/SSH remotes; require a webhook secret even on dev builds. **M**
- **P3.3 Operator key off the client** (`frontend/src/lib/api/core.ts:239-265`, `forven.ts:1280-1287`): never build-inline `VITE_FORVEN_OPERATOR_KEY`; terminate privileged auth at a backend session/proxy so it never reaches the browser; send the WS key post-connect, not in the URL. Also `safeHref(returnTo)` (`lab/strategy/[id]/+page.svelte:380`) and drop bare `https:` from `img-src` (`svelte.config.js:31`). **M**
- **P3.4 Egress** (`forven/research_sources/_http.py`, `agents/tools_research.py`): add an allowlist (or flag/limit) for agent-initiated fetches of non-registered hosts; document that positions/account data leave to the configured LLM vendor. **M**
- **P3.5 Data-at-rest** (`forven/db.py:433`, `forven/data.py:394`, `forven/sim/data_pump.py:22`): `chmod 0600` + icacls the SQLite DB on multi-user hosts; remove the `pd.read_pickle` lake fallback (match `revisions.py`); validate `asset`/`interval` identifiers before DDL. **S**
- **P3.6 Dependency pinning** (`pyproject.toml`, `forven/preflight.py:137`): use the frozen/hash-locked requirements for the editable preflight install so a future malicious dep release isn't auto-pulled. **S**

---

## Phase 4 — Verification & regression

- **P4.1 Sandbox-escape corpus:** every confirmed payload (pandas→subprocess, `from`-import smuggling, format-string `__globals__` read, `_winapi`/`http.client`/`pdb.run` gadgets) becomes a test asserting **both** that the allowlist rejects it **and** that the Phase-2 worker contains it (empty env, no FS escape, no egress).
- **P4.2 CSRF/Origin tests:** cross-origin unsafe requests rejected; local launcher path preserved.
- **P4.3 Secret-leak tests:** redactor field coverage; no endpoint serializes a decrypted secret.
- **P4.4 CI security gate:** run the above on every PR; optionally a `bandit`/`semgrep` pass scoped to the agent/strategy/api surfaces.
**Effort:** M.

---

## Decisions needed

1. ~~**Networked/multi-user in scope?**~~ **RESOLVED 2026-06-28: NO — single-user/localhost only for now.** So P3.3 (client key), P1.6 (operator key), and P1.3 (CORS) stay at their current (lower) priority; no per-user auth model needed yet. Revisit if that changes.
2. **Free Python vs `rule_engine` for autonomous agents.** Recommended: **autonomous agent path → `rule_engine` spec only**; free-Python strategies allowed only via operator-approved authoring, and always executed in the Phase-2 worker. This is the single biggest structural risk reduction.
3. **Windows worker isolation depth** (P2.1): baseline (env-scrub + FS-confine + socket-deny) now, OS-token/AppContainer sandbox as a follow-up — or invest in the stronger sandbox up front?

---

## Explicitly NOT doing (anti-bloat)

These don't map to a real finding for a localhost-desktop trading app and would be security theater: full WAF/SIEM, blanket per-endpoint rate-limiting, 2FA/SSO, mTLS between local components, secret-manager infra (the Fernet-at-rest + OS keystore path is adequate for the model). Revisit only if the networked/multi-user decision flips.
