"""Best-effort execution outcomes correlated with scanner evaluations."""

import json

from forven.db import get_db_best_effort, record_signal_result


def trade_snapshot(strategy_id: str) -> dict[str, dict] | None:
    try:
        with get_db_best_effort() as conn:
            return {row["id"]: dict(row) for row in conn.execute(
                "SELECT id, status, fill_entry_price, fill_exit_price FROM trades "
                "WHERE COALESCE(strategy_id, strategy) = ?", (strategy_id,),
            )}
    except Exception:
        return None


def record_execution_outcome(item: dict, before: dict | None, after: dict | None, diagnostic: dict, actions: list[str]) -> dict:
    """Use persisted fills/status transitions, never an action message as proof."""
    if before is None or after is None:
        return {"outcome": "unknown", "reason": "Execution ledger read unavailable"}
    entries = [key for key, row in after.items() if row.get("fill_entry_price")
               and row.get("status") != "FAILED" and not before.get(key, {}).get("fill_entry_price")]
    exits = [key for key, row in after.items() if row.get("status") == "CLOSED"
             and row.get("fill_exit_price") and before.get(key, {}).get("status") != "CLOSED"]
    failed = [key for key, row in after.items() if row.get("status") == "FAILED"
              and before.get(key, {}).get("status") != "FAILED"]
    reason = diagnostic.get("blocked_reason") or diagnostic.get("reason")
    if not reason:
        reason = next((s for s in actions if any(word in s for word in ("BLOCKED", "FAILED", "SKIPPED", "HELD"))), None)
    has_failure = bool(failed or diagnostic.get("execution_decision") == "failed" or any("FAILED" in s for s in actions))
    outcome = "executed" if entries or exits else "failed" if has_failure else "blocked" if reason else "no_action"
    summary = {"outcome": outcome, "entry_trade_ids": entries, "exit_trade_ids": exits, "failed_trade_ids": failed}
    ids = item.get("signal_result_ids") or {}
    for kind, trades in (("entry", entries), ("exit", exits)):
        row_id = ids.get(kind)
        if not row_id and not trades and not (failed and kind == "entry"):
            continue
        metrics = {**summary, "trade_ids": trades}
        block_reason = None if trades else str(reason or ("execution_failed" if failed else "no_actionable_position_or_order"))
        try:
            if row_id:
                with get_db_best_effort() as conn:
                    conn.execute(
                        "UPDATE scanner_signal_results SET executed=?, block_reason=?, "
                        "metrics_json=json_patch(COALESCE(metrics_json,'{}'),?) WHERE id=?",
                        (int(bool(trades)), block_reason, json.dumps(metrics), row_id),
                    )
            else:
                record_signal_result(item["strategy_id"], item["strategy"]["asset"], kind,
                                     matched=True, executed=bool(trades), block_reason=block_reason, metrics=metrics)
        except Exception:
            pass
    return summary
