# Upgrade notes

How updating a self-hosted Forven install behaves, and the few things worth knowing.

> **One recommendation up front: back up your database before updating.** It lives at
> `~/.forven/forven.db`. Schema migrations apply automatically and are idempotent, but a
> backup is cheap insurance — especially if you're jumping across several versions.
> `cp ~/.forven/forven.db ~/.forven/forven.db.bak-$(date +%s)`

---

## How updates arrive

Forven offers updates as **releases**: one tested version a week (Mondays), each with short
notes grouped as New, Fixes and Safety. A safety fix that can't wait ships the same day as a
small patch release marked **Urgent**.

- A pill in the top bar ("v0.2.0 available") opens **What's new** with the notes and an
  **Update now** button. Updating shows each step (Download, Install, Restart, Back online)
  and a Retry button if a step fails. *Remind me tomorrow* hides the pill for a day (urgent
  releases always show, with a one-line banner).
- **Settings > System > Updates** shows the installed version, a release history, and the
  update channel: **Stable** (releases only, the default) or **Latest** (every change as soon
  as it lands, for testers). `FORVEN_UPDATE_CHANNEL` in `.env` overrides the setting.
- Forven checks for updates at most once a day. Updating is a git fast-forward, so it needs
  only git, which every install already has. Your own untracked files don't block it; edits
  to Forven's tracked files do, with a message saying so.

---

## Engine safety + correctness release (2026-06)

A full audit of the paper + live trading engine fixed both critical findings and all 19
high-severity findings (kernel-path safety parity, promotion-gate integrity, restart/freeze
recovery, live-exchange edge cases).

### What happens automatically (no action needed)

- **Schema migrations self-apply on first launch.** `init_db()` runs them, tracks them in a
  `schema_migrations` table, and skips any already applied — so new columns/indexes install
  themselves on startup. No manual step.
- **The paper-engine "history flood" is auto-prevented.** On first boot, a migration stamps
  your paper book's go-live to *now*, so the corrected engine records new paper trades **from
  the upgrade forward** instead of replaying (and double-counting) your entire prior history.
  Fresh installs are untouched.
- **The promotion gate self-heals.** It now counts only trades recorded by the corrected
  engine, so your *existing* paper trades are simply ignored (never corrupted, never counted)
  and fresh evidence accumulates from the upgrade onward.

### What to understand (not "do")

- **Promotion progress effectively restarts.** Because pre-upgrade paper trades no longer count
  toward "go live," a strategy that looked almost ready will need to build fresh paper evidence
  on the corrected engine before it promotes. Nothing is broken — this is deliberate: strategies
  should never be promoted to real money on numbers from the old (broken) engine.

### Recommended, but optional

- **Clean-slate the paper book** so dashboards/equity stop showing old broken-engine trades.
  The auto-migration already prevents the flood; this just clears the stale trades from view:
  ```
  python scripts/reset_paper_trades.py            # dry-run report (shows what would be cleared)
  python scripts/reset_paper_trades.py --apply    # backs up the DB, then clears paper trades; KEEPS live trades
  ```
  Tip: pause/Emergency-Halt the paper service (or stop the daemon) before `--apply` so the
  scanner doesn't open a fresh trade mid-reset.
- **Re-score backtest metrics** (stored metrics were computed on the old engine and are stale).
  You can do this per strategy by re-running its backtest in the app/MCP (no downtime), or in
  bulk with `scripts/rescore_paper_strategies.py --apply` / `scripts/assign_execution_profiles.py
  --apply --force`. Note: the bulk scripts re-download market data per strategy and can take
  hours for a large population — run them with the daemon stopped, ideally overnight. The kernel
  default sizing is sane in the meantime, so this is a refinement, not a blocker.

### Behavior changes to expect (awareness only)

- **More timeframes now trade.** Strategies on 30m / 2h / 3m / 8h / 12h (and others) that were
  *silently never trading* in live/paper will start executing. Glance at your active roster after
  upgrading — a previously "dead" strategy may become active.
- **Stricter safety = possibly new alerts.** A hung scanner or frozen-but-alive daemon now goes
  RED and alerts; stale-data periods skip new entries; orphan exchange positions get an emergency
  stop; the kill-switch can no longer be silently lifted by a transient outage. New alerts you
  hadn't seen before are the system working, not breaking.
- **Brief startup gate.** On boot, *new live entries* are blocked for a few seconds until the
  daemon confirms the exchange is aligned (open positions stay protected by their resting stops).
  This self-clears.

### New operator-tunable settings (sane defaults; change only if needed)

- `daemon_tick_max_stale_seconds` (default 600) — how stale the daemon's heartbeat may get before
  it's flagged frozen.
- `reconcile_empty_read_confirm_count` (default 2) — consecutive empty exchange reads required
  before the reconciler will treat live positions as genuinely closed (guards against a glitchy
  empty read mass-closing real positions).
- `scanner_max_candle_staleness_bars` (default 2) — how many bars past close before candle data
  is treated as stale and new entries are blocked.

---

## Security hardening release (2026-06)

A security audit closed an in-process strategy-import RCE chain and tightened the sandbox. Most
of it is transparent; a few things are worth knowing.

### What happens automatically (no action needed)

- **Custom-strategy imports are auto-migrated.** The strategy sandbox no longer lets untrusted
  code import `forven.scanner` (it re-exports the database handle, secret decryption, and a
  live-order sink). Its *pure indicator* helpers (`rsi`, `atr`, `adx`, `stochastic`, …) now live
  behind the allowlisted `forven.strategies.indicators` facade. A one-time startup migration
  rewrites your own `custom/` strategies' `from forven.scanner import rsi, atr, …` to
  `from forven.strategies.indicators import …`, so a strategy that used those indicators keeps
  working with no edit. (Shipped builtins/composites were migrated in the release itself.)
- **Code-bundled strategy import runs out-of-process.** Importing a *shared* strategy that bundles
  Python now executes that code only inside a locked-down worker (no DB, no network, no secrets),
  never in the app process. Import/backtest still works — it's just contained.

### Behavior changes to expect (awareness only)

- **Cross-asset / self-fetching custom strategies stop loading.** A custom strategy that fetched
  its OWN market data inside the strategy — `from forven.data import …`, `from forven.data_manager
  import …`, `from forven.strategies.sentiment import fetch_funding_rates`, or `from forven.scanner
  import fetch_candles` — is now refused: the sandbox can't supply a second asset's series, so it
  would run on incomplete data. These are **not** auto-migrated (there is no safe rewrite). If you
  have one, it needs reworking to take its data from the engine (single-asset funding/OI is already
  enriched for you) or it stays inactive. The pure-indicator case above is the common one and is
  handled for you.
- **Live concurrency default bumps 1 → 5.** A migration rewrites a *persisted* live
  `max_concurrent_positions` of exactly `1` (the old default) to `5`; a value you set yourself is
  left alone. (Live trades **testnet** by default; mainnet stays gated behind `FORVEN_ALLOW_MAINNET`.)
- **Mainnet requires an agent wallet.** Live trading on mainnet — only when you explicitly set
  `FORVEN_ALLOW_MAINNET=1` — now refuses a master-account private key and requires a verified
  agent/API wallet, so even a full compromise cannot move funds off the platform. Testnet is
  unaffected.
