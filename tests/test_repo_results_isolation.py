"""Tests must never write to or empty the checkout's data/results.

2026-11-17: a full test run inside a live checkout called factory_reset(),
which emptied <repo>/data/results and lost every saved backtest trade list.
"""

from __future__ import annotations

from pathlib import Path

import forven.config as cfg

REPO_RESULTS = Path(cfg.__file__).resolve().parent.parent / "data" / "results"


def test_the_suite_points_repo_results_at_a_temp_folder():
    assert cfg.repo_results_dir() != REPO_RESULTS
    assert cfg.FORVEN_HOME in cfg.repo_results_dir().parents


def test_backtest_artifacts_are_written_under_the_temp_folder():
    from forven.backtest_api import _ensure_result_data_dir, _result_data_dirs

    target = Path(_ensure_result_data_dir())
    assert REPO_RESULTS not in (target, *target.parents)
    assert all(Path(d) != REPO_RESULTS for d in _result_data_dirs())


def test_factory_reset_leaves_the_checkouts_results_alone(forven_db, monkeypatch, tmp_path):
    import forven.db as db_mod

    real_repo_results = tmp_path / "checkout" / "data" / "results"
    real_repo_results.mkdir(parents=True)
    keep = real_repo_results / "S1_trades.json"
    keep.write_text("[]", encoding="utf-8")

    temp_results = cfg.repo_results_dir()
    temp_results.mkdir(parents=True, exist_ok=True)
    wiped = temp_results / "S2_trades.json"
    wiped.write_text("[]", encoding="utf-8")

    db_mod.factory_reset([])

    assert keep.exists()
    assert not wiped.exists()


def test_without_the_override_it_is_the_checkouts_folder(monkeypatch):
    monkeypatch.delenv("FORVEN_REPO_RESULTS_DIR", raising=False)
    assert cfg.repo_results_dir() == REPO_RESULTS
