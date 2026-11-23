"""A scan that finds nothing to act on must not write a log line per strategy.

The no-signal line was the largest single source in the backend log (about
200k lines a day on a 440-strategy fleet); the evaluation is already stored in
scanner_signal_results.
"""

from __future__ import annotations

import logging

from forven import scanner


def _row(strategy_id: str, *, entry: bool = False, exit_: bool = False) -> dict:
    return {
        "strategy_id": strategy_id,
        "strategy": {"asset": "BTC"},
        "signal": {"price": 100.0, "adx": 20.0, "entry_signal": entry, "exit_signal": exit_},
    }


def test_no_signal_evaluations_log_at_debug(monkeypatch, caplog):
    rows = [_row("S-quiet"), _row("S-entry", entry=True), _row("S-exit", exit_=True)]
    monkeypatch.setattr(scanner, "_scan_asset_group", lambda *args, **kwargs: rows)
    monkeypatch.setattr("forven.db.record_signal_result", lambda *args, **kwargs: 1)

    strategies = {row["strategy_id"]: {"asset": "BTC"} for row in rows}
    with caplog.at_level(logging.DEBUG, logger=scanner.log.name):
        signals, signal_rows = scanner._evaluate_signal_matrix(strategies, {}, {}, {})

    assert set(signals) == {"S-quiet", "S-entry", "S-exit"}
    assert len(signal_rows) == 3
    levels = {
        record.args[0]: record.levelno
        for record in caplog.records
        if record.name == scanner.log.name and "| ADX=" in record.msg
    }
    assert levels == {"S-quiet": logging.DEBUG, "S-entry": logging.INFO, "S-exit": logging.INFO}
