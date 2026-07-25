from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from forven.api_core import BacktestSubmitBody, post_backtest_submit
from forven.db import create_strategy_container, get_db
from forven.routers import strategies as strategies_router


def _configure_chart_paths(home: Path, monkeypatch) -> Path:
	import forven.api_core as api_core
	import forven.data as data_mod

	data_dir = home / "data"
	results_dir = data_dir / "results"
	results_dir.mkdir(parents=True, exist_ok=True)

	monkeypatch.setattr(data_mod, "DATA_DIR", data_dir)
	monkeypatch.setattr(api_core, "_result_data_dirs", lambda: [str(results_dir)])
	monkeypatch.setattr(api_core, "_ensure_result_data_dir", lambda: str(results_dir))
	return results_dir


def _seed_strategy(symbol: str = "BTC", strategy_type: str = "macd", params: dict | None = None) -> str:
	with get_db() as conn:
		strategy_id, _, _ = create_strategy_container(
			conn=conn,
			name="ignored",
			type_=strategy_type,
			symbol=symbol,
			timeframe="1h",
			params=params or {"fast": 12, "slow": 26, "signal": 9},
		)
	return strategy_id


def _seed_local_candles(symbol: str = "BTC", timeframe: str = "1h") -> None:
	import forven.data as data_mod

	timestamps = pd.date_range("2025-01-01T00:00:00+00:00", periods=600, freq="h", tz="UTC")
	frame = pd.DataFrame(
		{
			"timestamp": timestamps,
			"open": [100 + (index * 0.1) for index in range(len(timestamps))],
			"high": [101 + (index * 0.1) for index in range(len(timestamps))],
			"low": [99 + (index * 0.1) for index in range(len(timestamps))],
			"close": [100.5 + (index * 0.1) for index in range(len(timestamps))],
			"volume": [1000 + index for index in range(len(timestamps))],
		}
	)
	data_mod.save_parquet(frame, symbol, timeframe)


def _sample_trades() -> list[dict]:
	return [
		{
			"entry_time": "2025-01-10T08:00:00+00:00",
			"entry_price": 122.4,
			"exit_time": "2025-01-11T14:00:00+00:00",
			"exit_price": 126.2,
			"pnl": 310.0,
			"return_pct": 0.031,
		},
		{
			"entry_time": "2025-01-13T04:00:00+00:00",
			"entry_price": 128.0,
			"exit_time": "2025-01-14T18:00:00+00:00",
			"exit_price": 131.4,
			"pnl": 275.0,
			"return_pct": 0.026,
		},
	]


def _insert_result_row(
	*,
	result_id: str,
	strategy_id: str,
	strategy_name: str,
	strategy_type: str,
	symbol: str = "BTC",
	timeframe: str = "1h",
	start_date: str = "2025-01-10T00:00:00+00:00",
	end_date: str = "2025-01-20T00:00:00+00:00",
	params: dict | None = None,
) -> None:
	with get_db() as conn:
		conn.execute(
			"""
			INSERT INTO backtest_results
			(result_id, strategy_id, result_type, symbol, timeframe, start_date, end_date, metrics_json, config_json, created_at)
			VALUES (?, ?, 'backtest', ?, ?, ?, ?, ?, ?, ?)
			""",
			(
				result_id,
				strategy_id,
				symbol,
				timeframe,
				start_date,
				end_date,
				json.dumps(
					{
						"total_return_pct": 0.17,
						"sharpe": 1.42,
						"win_rate": 58.0,
						"max_drawdown_pct": 0.11,
						"profit_factor": 1.6,
						"total_trades": 2,
					}
				),
				json.dumps(
					{
						"strategy_id": strategy_id,
						"strategy_name": strategy_name,
						"strategy_type": strategy_type,
						"symbol": symbol,
						"timeframe": timeframe,
						"start": start_date,
						"end": end_date,
						"params": params or {},
						"job_id": f"job-{result_id}",
					}
				),
				"2026-03-11T00:00:00+00:00",
			),
		)


def test_chart_context_returns_artifact_for_new_backtests(forven_db, _isolate_forven_home, monkeypatch):
	results_dir = _configure_chart_paths(_isolate_forven_home, monkeypatch)
	_seed_local_candles()
	strategy_id = _seed_strategy()

	import forven.strategies.backtest as backtest_mod

	monkeypatch.setattr("forven.quant_skills_extractor.record_backtest_for_learning", lambda **_kwargs: None)
	monkeypatch.setattr(
		backtest_mod,
		"backtest_strategy",
		lambda **_kwargs: {
			"metrics": {
				"total_return_pct": 0.17,
				"sharpe": 1.42,
				"win_rate": 58.0,
				"max_drawdown_pct": 0.11,
				"profit_factor": 1.6,
				"total_trades": 2,
			},
			"trades": _sample_trades(),
			"start_date": "2025-01-10T00:00:00+00:00",
			"end_date": "2025-01-20T00:00:00+00:00",
		},
	)

	response = post_backtest_submit(
		BacktestSubmitBody(
			strategy_id=strategy_id,
			symbol="BTC",
			timeframe="1h",
			start="2025-01-10T00:00:00+00:00",
			end="2025-01-20T00:00:00+00:00",
		)
	)

	assert response["status"] == "succeeded"
	result_id = response["result_id"]
	assert any(results_dir.glob(f"{result_id}_chart.json"))

	context = strategies_router.get_backtest_result_chart_context(result_id, remote_skip=True)
	assert context["source"] == "artifact"
	assert context["result_id"] == result_id
	assert len(context["bars"]) > 0
	assert len(context["entry_markers"]) == 2
	assert len(context["exit_markers"]) == 2
	assert [indicator["name"] for indicator in context["sub_indicators"]] == ["MACD", "Signal"]
	assert context["strategy_params"]["fast"] == 12


def test_chart_context_recomputes_for_snapshotless_builtin_run(forven_db, _isolate_forven_home, monkeypatch):
	_configure_chart_paths(_isolate_forven_home, monkeypatch)
	_seed_local_candles()

	import forven.api_core as api_core

	strategy_id = _seed_strategy(strategy_type="macd", params={"fast": 8, "slow": 21, "signal": 5})
	_insert_result_row(
		result_id="snapshotless-macd",
		strategy_id=strategy_id,
		strategy_name="BTC-MACD-SNAPSHOTLESS",
		strategy_type="macd",
		params={"fast": 8, "slow": 21, "signal": 5},
	)
	api_core._write_backtest_result_artifacts("snapshotless-macd", "job-snapshotless-macd", _sample_trades())

	context = strategies_router.get_backtest_result_chart_context("snapshotless-macd", remote_skip=True)

	assert context["source"] == "recomputed"
	assert len(context["bars"]) > 0
	assert len(context["entry_markers"]) == 2
	assert len(context["exit_markers"]) == 2
	assert [indicator["name"] for indicator in context["sub_indicators"]] == ["MACD", "Signal"]
	assert context["warnings"] == []


def test_chart_context_skips_audit_lookup_when_result_payload_already_resolves_context(forven_db, _isolate_forven_home, monkeypatch):
	_configure_chart_paths(_isolate_forven_home, monkeypatch)
	_seed_local_candles()

	import forven.api_core as api_core

	strategy_id = _seed_strategy(strategy_type="macd", params={"fast": 8, "slow": 21, "signal": 5})
	_insert_result_row(
		result_id="snapshotless-no-audit",
		strategy_id=strategy_id,
		strategy_name="BTC-MACD-NO-AUDIT",
		strategy_type="macd",
		params={"fast": 8, "slow": 21, "signal": 5},
	)
	api_core._write_backtest_result_artifacts("snapshotless-no-audit", "job-snapshotless-no-audit", _sample_trades())

	def _unexpected_audit_lookup(_strategy_id: str):
		raise AssertionError("chart reconstruction should not hit task audit when result + strategy rows are enough")

	monkeypatch.setattr(api_core, "_infer_strategy_context_from_task_audit", _unexpected_audit_lookup)

	context = strategies_router.get_backtest_result_chart_context("snapshotless-no-audit", remote_skip=True)

	assert context["source"] == "recomputed"
	assert len(context["bars"]) > 0
	assert [indicator["name"] for indicator in context["sub_indicators"]] == ["MACD", "Signal"]
	assert context["warnings"] == []


def test_chart_context_gracefully_falls_back_for_unsupported_strategy(forven_db, _isolate_forven_home, monkeypatch):
	_configure_chart_paths(_isolate_forven_home, monkeypatch)
	_seed_local_candles()

	import forven.api_core as api_core

	strategy_id = _seed_strategy(strategy_type="custom_alpha", params={"threshold": 1.5})
	_insert_result_row(
		result_id="unsupported-custom",
		strategy_id=strategy_id,
		strategy_name="BTC-CUSTOM-ALPHA",
		strategy_type="custom_alpha",
		params={"threshold": 1.5},
	)
	api_core._write_backtest_result_artifacts("unsupported-custom", "job-unsupported-custom", _sample_trades())

	context = strategies_router.get_backtest_result_chart_context("unsupported-custom", remote_skip=True)

	assert context["source"] == "recomputed"
	assert len(context["bars"]) > 0
	assert len(context["entry_markers"]) == 2
	assert context["main_indicators"] == []
	assert context["sub_indicators"] == []
	assert any("unavailable" in warning.lower() for warning in context["warnings"])


def test_chart_context_warns_when_local_ohlcv_is_missing(forven_db, _isolate_forven_home, monkeypatch):
	_configure_chart_paths(_isolate_forven_home, monkeypatch)

	import forven.api_core as api_core

	# create_strategy_container refuses a symbol that is in neither the data lake
	# nor the symbol registry, and this test's whole point is that the 1h series is
	# absent. Seed BTC at a DIFFERENT timeframe so the symbol is known to be a real
	# market, while the 1h series the chart asks for stays genuinely missing.
	_seed_local_candles(symbol="BTC", timeframe="4h")
	strategy_id = _seed_strategy(strategy_type="macd", params={"fast": 12, "slow": 26, "signal": 9})
	_insert_result_row(
		result_id="missing-ohlcv",
		strategy_id=strategy_id,
		strategy_name="BTC-MACD-MISSING",
		strategy_type="macd",
		params={"fast": 12, "slow": 26, "signal": 9},
	)
	api_core._write_backtest_result_artifacts("missing-ohlcv", "job-missing-ohlcv", _sample_trades())

	context = strategies_router.get_backtest_result_chart_context("missing-ohlcv", remote_skip=True)

	assert context["source"] == "recomputed"
	assert context["bars"] == []
	assert len(context["entry_markers"]) == 2
	assert any("no local ohlcv" in warning.lower() for warning in context["warnings"])


def test_chart_context_fast_fails_when_pyarrow_is_unavailable_for_parquet(forven_db, _isolate_forven_home, monkeypatch):
	_configure_chart_paths(_isolate_forven_home, monkeypatch)

	import forven.api_core as api_core
	import forven.data as data_mod

	monkeypatch.setattr(data_mod, "pa", None)
	monkeypatch.setattr(data_mod, "pq", None)

	parquet_file = data_mod.parquet_path("BTC", "1h")
	parquet_file.parent.mkdir(parents=True, exist_ok=True)
	parquet_file.write_bytes(b"PAR1mock-chart-data")

	strategy_id = _seed_strategy(strategy_type="macd", params={"fast": 12, "slow": 26, "signal": 9})
	_insert_result_row(
		result_id="missing-pyarrow-chart",
		strategy_id=strategy_id,
		strategy_name="BTC-MACD-NO-PYARROW",
		strategy_type="macd",
		params={"fast": 12, "slow": 26, "signal": 9},
	)
	api_core._write_backtest_result_artifacts("missing-pyarrow-chart", "job-missing-pyarrow-chart", _sample_trades())

	context = strategies_router.get_backtest_result_chart_context("missing-pyarrow-chart", remote_skip=True)

	assert context["source"] == "recomputed"
	assert context["bars"] == []
	assert len(context["entry_markers"]) == 2
	assert any("pyarrow" in warning.lower() for warning in context["warnings"])


def test_chart_context_fetches_remote_ohlcv_when_local_dataset_is_unreadable(forven_db, _isolate_forven_home, monkeypatch):
	_configure_chart_paths(_isolate_forven_home, monkeypatch)

	import forven.api_core as api_core
	import forven.data as data_mod

	# NOTE: pyarrow is deliberately NOT disabled here. "Unreadable" in this test
	# means a CORRUPT parquet file (the broken bytes below); the refetched frame
	# still has to be written and read back through pyarrow, so nulling data_mod.pa
	# / data_mod.pq would make the remote fetch unreadable too and yield 0 bars.
	# The pyarrow-missing case is covered by the preceding test.
	unreadable_path = data_mod.parquet_path("DOT/USDT", "1h")
	unreadable_path.parent.mkdir(parents=True, exist_ok=True)
	unreadable_path.write_bytes(b"PAR1broken-dot-data")

	def _mock_fetch_ohlcv_chunked(symbol: str, timeframe: str, **_kwargs):
		timestamps = pd.date_range("2025-06-01T00:00:00+00:00", periods=1200, freq="h", tz="UTC")
		frame = pd.DataFrame(
			{
				"timestamp": timestamps,
				"open": [10 + (index * 0.01) for index in range(len(timestamps))],
				"high": [10.1 + (index * 0.01) for index in range(len(timestamps))],
				"low": [9.9 + (index * 0.01) for index in range(len(timestamps))],
				"close": [10.05 + (index * 0.01) for index in range(len(timestamps))],
				"volume": [500 + index for index in range(len(timestamps))],
			}
		)
		data_mod.save_parquet(frame, symbol, timeframe, source="binance")
		return {
			"symbol": symbol,
			"timeframe": timeframe,
			"source": "binance",
			"row_count": len(frame),
			"bars_fetched": len(frame),
			"bars_new": len(frame),
		}

	monkeypatch.setattr(data_mod, "fetch_ohlcv_chunked", _mock_fetch_ohlcv_chunked)

	strategy_id = _seed_strategy(symbol="DOT", strategy_type="stochastic", params={"k_period": 14, "d_period": 3})
	_insert_result_row(
		result_id="remote-dot-chart",
		strategy_id=strategy_id,
		strategy_name="DOT-STOCHASTIC-REMOTE",
		strategy_type="stochastic",
		symbol="DOT",
		timeframe="1h",
		start_date="2025-06-22T00:00:00+00:00",
		end_date="2025-07-10T00:00:00+00:00",
		params={"k_period": 14, "d_period": 3},
	)
	api_core._write_backtest_result_artifacts("remote-dot-chart", "job-remote-dot-chart", _sample_trades())

	context = strategies_router.get_backtest_result_chart_context("remote-dot-chart", remote_skip=True)

	assert context["source"] == "recomputed"
	assert len(context["bars"]) > 0
	assert any("fetched remote ohlcv" in warning.lower() for warning in context["warnings"])
