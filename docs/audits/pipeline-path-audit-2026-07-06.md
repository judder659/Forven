# Pipeline path audit — crucible creation → paper (2026-07-06)

Three parallel code audits (creation→dispatch, registration→gauntlet,
gauntlet→paper) + live-DB forensics over the 7-day funnel. Verdict: the spine
is sound and the recent fix waves held; six defects found and fixed same-day
(commit references below); a handful of accepted risks documented.

## Confirmed working (verified with evidence)

- All seven S05838 quick_screen→gauntlet fail-open gaps CLOSED (verdict
  defaults, cherry-pick subset, auto-handoff overwrite, canonical guard,
  creation precheck, research recovery, planner done-on-blocked).
- The 2026-07-03 "promotion_ready:true over persisted WFA FAIL" landmine is
  FIXED under default config (531e0943: insufficient-fold evidence blocks,
  passed=False authoritative, errored rows read as absence, in-flight
  validation blocks promotion). Covered by tests.
- No unauthorized path to paper: every promotion routes through
  _evaluate_gauntlet_gate; MCP/agent force is neutered for capital stages;
  approvals never front-run the gate; dethrone can only demote.
- Crucible creation/refine/dispatch spine: stamping, budget + quota counting,
  directive delivery (both channels), in-flight caps, cross-loop dedup.
- 7-day funnel forensics: the scary historical shapes (WFA-FAIL fast-path
  promotions, failed_gate re-promotions) all PREDATE the 531e0943 guards,
  which only went live at the 2026-07-05 restart.

## Fixed in this audit

1. **CRUX-1 exploit-lane short-circuit** (crucible_planner): the
   proven+protected one-shot block ran before the survivor lane; since
   proven auto-protects, the HIGHEST-value crucibles were capped at one
   lifetime expansion. Survivor lane now runs first.
2. **CRUX-1 child-signal linkage blind spot** (crucible_allocator +
   hypothesis_promotion): scoring joined hypothesis_id only, missing
   origin_crucible_id-linked survivors. Now COALESCE-linked like
   _strategy_count.
3. **trade_mode silently lost** (db.create_strategy_container): a class whose
   supported_trade_modes excludes long_only was silently backtested long_only
   (resolver default). The single creation choke point now injects the
   class's preferred direction. Critical for the CRUX-1 short quota.
4. **Stale-passed-workflow fast-path re-promotion** (evolution): operator
   bulk demote-to-rerun was defeated within minutes — the fast path
   re-promoted off the OLD passed workflow's artifacts (S05198/S05407/
   S04925/S03151). Fast path now blocks when the passed workflow predates
   the current gauntlet entry; the workflow backfill's terminal-reset then
   drives the genuine re-run.
5. **Readiness report fail-open on required_tests=[]** (policy): the gate
   treats empty as enforce-ALL, but the readiness report skipped validation
   entirely → ready:true over a WFA FAIL (display-level false green, feeds
   MCP gate reports). Readiness now mirrors enforce-all.
6. **"window insufficient" drained to failed_gate** (gauntlet/tasks): an
   unjudgeable WFA at the workflow's paper gate archived the strategy
   instead of retrying on the trade-rate-sized window. Now retryable.

## Accepted risks / operator decisions (not fixed)

- **Relaxed-paper composition**: a strategy can reach paper with WFA
  fold-floor rescue (>=33% judgeable folds) while MC/jitter/regime are
  unjudgeable and cost_stress fails (non-required) — e.g. S05407 promoted on
  9 trades. Each step honors its configured leniency; the COMPOSITION is a
  rubber stamp. 19/46 of the week's promotions were fold-floor rescues.
  Recommend (operator call): a minimum-evidence floor at the paper gate —
  e.g. require >=1 judgeable non-WFA test, or make cost_stress FAIL blocking
  even at paper. The ~109-day evaluation window is the root cause
  (regime-aware judging / longer windows = the planned phase 3).
- evaluate_data_availability fails open on internal error (defended
  downstream); fruitless/refine detection is telemetry-coupled (degrades
  silently if tool-trace capture breaks); cross-loop dispatch dedup is
  non-transactional (bounded by budgets); add_job re-enables disabled
  planner jobs on restart; slot-competition starvation only if
  paper_slot_competition_enabled is turned on (default off); enforce-all +
  non-workflow path doesn't require artifact PRESENCE (non-default config);
  dead code check_quick_screen_zero_trade_rejection references a nonexistent
  table.
