"""Revive the wrongly-archived strategies from the 2026-07-22 audit.

Audit: outputs/archived-strategy-audit-2026-07-22.md. Four defect clusters
archived strategies on structural (non-merit) failures:

  1. Short-window workflow WFA (2026-07-18..20): the walk_forward step judged a
     ~4-month optimizer holdout whose OOS folds could not contain
     wfa_min_fold_trades at the strategy's measured cadence — verdict FAIL by
     construction. Fixed by WFA-WINDOW-2 (PR #103); the fix MUST be deployed
     (merged + backend restarted) before running this script, or the reset
     workflows re-run walk_forward on the same starved window and the sweep
     re-archives everything (proven live twice on S07680/81, 2026-07-20).
  2. "Runtime unloadable in paper" wave (2026-07-07): PAPER-stage strategies
     archived because their runtime class was not registered (orphan
     runtime_type, fixed in PR #76-79 four days later). Never revived.
  3. cost_stress ordering-violation loop: workflows stuck re-running
     cost_stress against the ordering guard until the 2-day un-promotable
     hygiene sweep archived them (S06895: 253 rejections).
  4. tier2 "no runtime class registered" (2026-07-19/20): post-fix recurrence
     of the registration-loss class.

For each candidate this script:
  * verifies the runtime class resolves TODAY (runtime_unloadable_reason —
    the same check the paper runtime and pipeline_sweep use); skips otherwise;
  * resets the latest current-version gauntlet workflow in place to ``pending``
    (steps zeroed) — create_or_get_workflow reuses terminal workflows and the
    self-heal backfill deliberately skips ``failed_gate`` ones, so without this
    reset demote_failed_gate_strategies re-archives the strategy on its next
    tick off the stale terminal workflow;
  * transitions archived -> quick_screen via brain.transition_stage (the
    sanctioned, event-logged path) so the gauntlet re-adjudicates from scratch
    under the current engine. No force-promotion anywhere.

Usage:
  python scripts/revive_wrongly_archived_2026_07_22.py             # dry-run
  python scripts/revive_wrongly_archived_2026_07_22.py --execute --backend-restarted
  python scripts/revive_wrongly_archived_2026_07_22.py --all ...   # include weak-stat tail
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

# Cluster 1 — short-window WFA victims worth compute (audit table order).
CLUSTER1 = [
    "S07680", "S07681", "S07686", "S07674", "S07690", "S07707", "S07699", "S07676",
]
# Cluster 1 tail — archived in the same wave with weak/noisy stats; --all only.
CLUSTER1_TAIL = [
    "S07664", "S07673", "S07670", "S07677", "S07702", "S07703", "S07684",
    "S07693", "S07698", "S07683", "S07695", "S07696", "S07675", "S07701",
]
# Cluster 3 — ordering-violation loop victims.
CLUSTER3 = ["S06895", "S05904", "S06135"]
# Cluster 4 — tier2 no-runtime-class notables (loadable as of 2026-07-22).
CLUSTER4 = ["S06164", "S06155", "S07466", "S05074", "S07018"]
# Individual: 1-fold WFA reject on the same window class (verify the lookahead
# stamp with the tier2 probe before any paper promotion — audit note).
INDIVIDUAL = ["S03524"]
# Cluster 2 — runtime-unloadable paper wave, loadable subset with real stats.
CLUSTER2 = [
    "S04369", "S03517", "S03171", "S03523", "S03219", "S06023", "S03184",
    "S03493", "S03151", "S03222", "S06030", "S03106", "S01598", "S05799",
]
# Cluster 2 tail — weak stats; --all only.
CLUSTER2_TAIL = ["S05275", "S05276"]

REASON = (
    "Operator-approved revival (archived-strategy audit 2026-07-22, "
    "outputs/archived-strategy-audit-2026-07-22.md): archived on a structural "
    "non-merit failure ({cause}). Workflow reset for full re-adjudication under "
    "the current engine with WFA-WINDOW-2 (PR #103) deployed."
)
CAUSES = {
    **{sid: "short-window workflow WFA noise verdict, 2026-07-18..20 wave" for sid in CLUSTER1 + CLUSTER1_TAIL},
    **{sid: "cost_stress ordering-violation loop -> un-promotable hygiene archive" for sid in CLUSTER3},
    **{sid: "tier2 runtime-class registration loss" for sid in CLUSTER4},
    **{sid: "1-fold WFA reject on a fold-starving window" for sid in INDIVIDUAL},
    **{sid: "runtime unloadable in paper (orphan runtime_type, pre-PR #76-79)" for sid in CLUSTER2 + CLUSTER2_TAIL},
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--execute", action="store_true", help="apply changes (default: dry-run)")
    parser.add_argument(
        "--backend-restarted", action="store_true",
        help="acknowledge the backend was restarted AFTER PR #103 merged (required with --execute)",
    )
    parser.add_argument("--all", action="store_true", help="include the weak-stat tail candidates")
    parser.add_argument("--only", help="comma-separated strategy ids to restrict to")
    args = parser.parse_args()

    # The fix must exist in this checkout; the backend must run it too.
    try:
        from forven.gauntlet.tasks import _dated_wfa_window_issue  # noqa: F401
    except ImportError:
        print("REFUSING: forven.gauntlet.tasks._dated_wfa_window_issue not found — "
              "this checkout predates WFA-WINDOW-2 (PR #103). Pull main first.")
        return 2
    if args.execute and not args.backend_restarted:
        print("REFUSING: --execute requires --backend-restarted. The gauntlet sweep runs "
              "inside the backend process; without the restarted fix it re-runs "
              "walk_forward on the starved window and re-archives every revival "
              "(proven on S07680/81, 2026-07-20).")
        return 2

    from forven.brain import transition_stage
    from forven.db import get_db
    from forven.gauntlet.engine import _reset_workflow_to_pending
    from forven.gauntlet.store import WORKFLOW_DEFINITION_VERSION
    from forven.strategies.registry import runtime_unloadable_reason

    candidates = CLUSTER1 + CLUSTER3 + CLUSTER4 + INDIVIDUAL + CLUSTER2
    if args.all:
        candidates += CLUSTER1_TAIL + CLUSTER2_TAIL
    if args.only:
        wanted = {s.strip().upper() for s in args.only.split(",") if s.strip()}
        candidates = [sid for sid in candidates if sid in wanted]

    now = datetime.now(timezone.utc).isoformat()
    revived, skipped = [], []
    for sid in candidates:
        with get_db() as conn:
            row = conn.execute(
                "SELECT id, display_id, stage, type, runtime_type FROM strategies WHERE id = ?",
                (sid,),
            ).fetchone()
        if not row:
            skipped.append((sid, "not found"))
            continue
        if str(row["stage"] or "").lower() not in {"archived", "rejected"}:
            skipped.append((sid, f"already in stage {row['stage']} — leaving alone"))
            continue
        unloadable = runtime_unloadable_reason(row["type"], row["runtime_type"])
        if unloadable:
            skipped.append((sid, f"runtime still unloadable: {unloadable}"))
            continue

        with get_db() as conn:
            wf = conn.execute(
                """SELECT id, status FROM gauntlet_workflows
                   WHERE strategy_id = ? AND definition_version = ?
                   ORDER BY datetime(created_at) DESC LIMIT 1""",
                (sid, WORKFLOW_DEFINITION_VERSION),
            ).fetchone()

        if args.execute:
            # Reset the terminal workflow FIRST: demote_failed_gate_strategies keys
            # off status='failed_gate', so flipping it to pending before the stage
            # transition closes the re-archive race.
            if wf:
                with get_db() as conn:
                    _reset_workflow_to_pending(
                        conn, str(wf["id"]), now,
                        reason=REASON.format(cause=CAUSES.get(sid, "audit 2026-07-22")),
                    )
            transition_stage(
                sid, "quick_screen",
                reason=REASON.format(cause=CAUSES.get(sid, "audit 2026-07-22")),
                actor="ui",
            )
            revived.append(sid)
            print(f"REVIVED {sid} ({row['display_id']}) — workflow "
                  f"{wf['id'][:16] + ' reset' if wf else 'none (backfill will create one)'}")
        else:
            print(f"DRY-RUN would revive {sid} ({row['display_id']}, stage={row['stage']}, "
                  f"wf={wf['status'] if wf else 'none'}) — {CAUSES.get(sid, '')}")
            revived.append(sid)

    print(f"\n{'revived' if args.execute else 'would revive'}: {len(revived)}  skipped: {len(skipped)}")
    for sid, why in skipped:
        print(f"  SKIP {sid}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
