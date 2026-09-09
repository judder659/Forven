# Application reliability improvements

Forven now preserves the relationship between research evidence, strategy revisions,
and forward execution more explicitly, while making stalled work easier to diagnose.

## Research and strategy creation

- Creator revisions retain their selected market, timeframe, parameters and execution
  settings when handed to Forge. Input preflight explains known missing feeds before
  generation; it does not certify an arbitrary strategy's quality or tradability.
- Crucible intake exposes actionable input and creation failures, improves dialog
  focus and scrolling, and keeps candidate outcomes separate from agent task counts.
- Gauntlet work uses bounded workers, durable history, cancellation and retry checks,
  and recovery paths that preserve failed evidence rather than inventing a pass.
- The research queue can resume eligible candidates that stopped before model or
  tool execution once their required data becomes available.

## Execution evidence

Backtests capture the resolved code identity, parameters, market, timeframe, engine
version and execution assumptions. Forward execution uses the accepted snapshot and
rechecks it before a new entry. Later diagnostic backtests cannot silently replace
the evidence accepted at promotion. Sizing and accounting changes keep costs,
leverage, funding and execution chronology consistent across the shared kernel.

Existing live books with legacy admission records have an explicit operator repair
helper in `forven.strategies.live_revalidation`. It accepts only a completed,
matching backtest for an already-live strategy with an existing admission event.
The repair is recorded as a same-stage audit event and does not assert that a new
research gauntlet passed. It cannot promote research or paper strategies. Configuration
or source changes still require revalidation. No live-book migration runs automatically.

## Runtime and agent operation

Agent execution state and outcomes distinguish completed work, blocked work and
research results. Runtime diagnostics expose thread health and work locations;
deadlines and subprocess budgets bound expensive operations. Provider support and
workspace guidance have also been refreshed; existing operator model selections
remain configuration choices.

## Data maintenance and Windows startup

Archive discovery uses a bounded listing of the public Binance archive rather than
probing every month since launch. Stream failures are reported to the scheduler.
Long historical imports still have a finite scheduler allowance: a timeout is not
successful completion, and this change does not guarantee that every bulk import
fits within that allowance.

Knowledge consolidation preserves previous archive versions. The Windows desktop
launcher provides service controls and status. Ordinary frontend restarts preserve
Vite's dependency cache, and lazy UI dependencies are prepared together to avoid
replacing chunks while a page is loading. Vite still invalidates the cache when
dependencies or configuration change.

The obsolete Portfolio screen and its unused API client have been removed. Current
trading sessions and risk controls remain in their dedicated views.

Local account data, strategy databases, credentials, runtime artifacts and operator
review reports are not part of the application source distribution.
