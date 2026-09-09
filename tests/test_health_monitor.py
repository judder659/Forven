"""Tests for the unified health monitor."""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from forven.health_monitor import (
    ComponentStatus,
    DataCheck,
    HealthAlert,
    HealthMonitor,
    HealthState,
    Severity,
    State,
    HEALTH_CIRCUIT_BREAKER_COUNT,
    HEALTH_CIRCUIT_BREAKER_WINDOW,
    PIPELINE_FROZEN_THRESHOLD_MINUTES,
    _dispatch_alerts,
    _attempt_recovery,
    _parse_iso,
    compute_state,
    check_scheduler,
    check_brain_workers,
    check_bots,
    check_lab_worker,
    check_pipeline_consistency,
    check_pipeline_throughput,
    check_sqlite_health,
)

# Common mock target for notification emission
_EMIT_TARGET = "forven.notifications.emit_notification"


# ---------------------------------------------------------------------------
# HealthState CRUD
# ---------------------------------------------------------------------------

class TestHealthState:
    def test_update_and_get_component(self):
        state = HealthState()
        cs = ComponentStatus(name="sched", state=State.GREEN, message="ok")
        prev = state.update_component(cs)
        assert prev is None
        assert state.get_component("sched") is cs

    def test_update_returns_previous(self):
        state = HealthState()
        old = ComponentStatus(name="sched", state=State.GREEN)
        state.update_component(old)
        new = ComponentStatus(name="sched", state=State.RED, message="dead")
        prev = state.update_component(new)
        assert prev is old

    def test_get_all_statuses(self):
        state = HealthState()
        state.update_component(ComponentStatus(name="a", state=State.GREEN))
        state.update_component(ComponentStatus(name="b", state=State.AMBER))
        assert len(state.get_all_statuses()) == 2

    def test_overall_state_worst_wins(self):
        state = HealthState()
        assert state.get_overall_state() == State.GREEN  # empty
        state.update_component(ComponentStatus(name="a", state=State.GREEN))
        assert state.get_overall_state() == State.GREEN
        state.update_component(ComponentStatus(name="b", state=State.AMBER))
        assert state.get_overall_state() == State.AMBER
        state.update_component(ComponentStatus(name="c", state=State.RED))
        assert state.get_overall_state() == State.RED

    def test_alert_history_capped(self):
        state = HealthState()
        for i in range(150):
            state.record_alert(HealthAlert(
                severity=Severity.INFO, component="test", message=f"msg {i}",
            ))
        assert len(state.get_alerts()) == 100  # max

    def test_alert_filter_by_severity(self):
        state = HealthState()
        state.record_alert(HealthAlert(severity=Severity.CRITICAL, component="a", message="crit"))
        state.record_alert(HealthAlert(severity=Severity.INFO, component="b", message="info"))
        state.record_alert(HealthAlert(severity=Severity.WARNING, component="c", message="warn"))
        assert len(state.get_alerts(severity=Severity.CRITICAL)) == 1
        assert len(state.get_alerts(severity=Severity.INFO)) == 1

    def test_consecutive_warn_tracking(self):
        state = HealthState()
        assert state.get_warn_count("x") == 0
        assert state.increment_warn("x") == 1
        assert state.increment_warn("x") == 2
        state.clear_warn("x")
        assert state.get_warn_count("x") == 0

    def test_circuit_breaker(self):
        state = HealthState()
        assert not state.is_circuit_broken("bot:X")
        for _ in range(HEALTH_CIRCUIT_BREAKER_COUNT):
            state.record_recovery("bot:X")
        assert state.is_circuit_broken("bot:X")

    def test_data_check_store(self):
        state = HealthState()
        dc = DataCheck(name="candle:BTC", passed=True, detail="fresh")
        state.update_data_check(dc)
        checks = state.get_all_data_checks()
        assert len(checks) == 1
        assert checks[0].name == "candle:BTC"

    def test_was_recently_alerted(self):
        state = HealthState()
        assert not state.was_recently_alerted("key", cooldown_seconds=60)
        state.mark_notified("key")
        assert state.was_recently_alerted("key", cooldown_seconds=60)


# ---------------------------------------------------------------------------
# Data models serialization
# ---------------------------------------------------------------------------

class TestDataModels:
    def test_component_status_to_dict(self):
        cs = ComponentStatus(
            name="scheduler", state=State.GREEN,
            last_seen=datetime(2026, 3, 26, 12, 0, 0, tzinfo=timezone.utc),
            message="ok",
        )
        d = cs.to_dict()
        assert d["state"] == "green"
        assert d["name"] == "scheduler"
        assert "2026" in d["last_seen"]

    def test_data_check_to_dict(self):
        dc = DataCheck(name="wal", passed=False, severity=Severity.WARNING, detail="big")
        d = dc.to_dict()
        assert d["passed"] is False
        assert d["severity"] == "warning"

    def test_health_alert_to_dict(self):
        ha = HealthAlert(severity=Severity.CRITICAL, component="bot", message="dead")
        d = ha.to_dict()
        assert d["severity"] == "critical"
        assert "timestamp" in d


# ---------------------------------------------------------------------------
# compute_state threshold logic
# ---------------------------------------------------------------------------

class TestComputeState:
    def test_none_last_seen_is_red(self):
        assert compute_state(None, 60) == State.RED

    def test_fresh_is_green(self):
        now = datetime.now(timezone.utc)
        assert compute_state(now, 60) == State.GREEN

    def test_overdue_2x_is_amber(self):
        ago = datetime.now(timezone.utc) - timedelta(seconds=150)
        assert compute_state(ago, 60) == State.AMBER

    def test_overdue_5x_is_red(self):
        ago = datetime.now(timezone.utc) - timedelta(seconds=400)
        assert compute_state(ago, 60) == State.RED

    def test_exactly_at_amber_boundary_is_green(self):
        ago = datetime.now(timezone.utc) - timedelta(seconds=119)
        assert compute_state(ago, 60) == State.GREEN


# ---------------------------------------------------------------------------
# _parse_iso
# ---------------------------------------------------------------------------

class TestParseIso:
    def test_none(self):
        assert _parse_iso(None) is None

    def test_empty_string(self):
        assert _parse_iso("") is None

    def test_valid_iso(self):
        dt = _parse_iso("2026-03-26T12:00:00+00:00")
        assert dt is not None
        assert dt.year == 2026

    def test_naive_gets_utc(self):
        dt = _parse_iso("2026-03-26T12:00:00")
        assert dt.tzinfo == timezone.utc

    def test_datetime_passthrough(self):
        now = datetime.now(timezone.utc)
        assert _parse_iso(now) is now

    def test_epoch_seconds(self):
        now = time.time()
        parsed = _parse_iso(now)
        assert parsed is not None
        assert abs(parsed.timestamp() - now) < 1


# ---------------------------------------------------------------------------
# Alert dispatch
# ---------------------------------------------------------------------------

class TestAlertDispatch:
    def test_critical_fires_immediately(self):
        state = HealthState()
        old = {"scheduler": ComponentStatus(name="scheduler", state=State.GREEN)}
        new = {"scheduler": ComponentStatus(name="scheduler", state=State.RED, message="dead")}

        with patch(_EMIT_TARGET) as mock_emit:
            _dispatch_alerts(state, old, new)
            mock_emit.assert_called_once()
            call_kwargs = mock_emit.call_args
            assert call_kwargs[1]["severity"] == "critical"
            assert call_kwargs[1]["channel_name"] == "alerts"

    def test_warning_requires_consecutive(self):
        state = HealthState()
        old = {"scheduler": ComponentStatus(name="scheduler", state=State.GREEN)}
        new_amber = {"scheduler": ComponentStatus(name="scheduler", state=State.AMBER, message="slow")}

        with patch(_EMIT_TARGET) as mock_emit:
            # First GREEN -> AMBER transition — count=1, need 2, should NOT fire
            _dispatch_alerts(state, old, new_amber)
            mock_emit.assert_not_called()
            assert state.get_warn_count("scheduler") == 1

        # Second GREEN -> AMBER transition — count reaches 2
        # Use fresh patch so dedup from alert recording doesn't interfere
        with patch(_EMIT_TARGET) as mock_emit2:
            _dispatch_alerts(state, old, new_amber)
            assert state.get_warn_count("scheduler") == 2
            mock_emit2.assert_called_once()
            assert mock_emit2.call_args[1]["severity"] == "warn"

    def test_recovery_fires_info(self):
        state = HealthState()
        old = {"scheduler": ComponentStatus(name="scheduler", state=State.RED)}
        new = {"scheduler": ComponentStatus(name="scheduler", state=State.GREEN, message="back")}

        with patch(_EMIT_TARGET) as mock_emit:
            _dispatch_alerts(state, old, new)
            mock_emit.assert_called_once()
            assert mock_emit.call_args[1]["severity"] == "info"
            assert mock_emit.call_args[1]["channel_name"] == "heartbeat"

    def test_no_change_no_alert(self):
        state = HealthState()
        cs = ComponentStatus(name="scheduler", state=State.GREEN)
        old = {"scheduler": cs}
        new = {"scheduler": cs}

        with patch(_EMIT_TARGET) as mock_emit:
            _dispatch_alerts(state, old, new)
            mock_emit.assert_not_called()

    def test_dedup_prevents_repeated_critical(self):
        state = HealthState()
        # Simulate a recently sent CRITICAL notification (severity-scoped key)
        state.mark_notified("health_scheduler:critical")

        old = {"scheduler": ComponentStatus(name="scheduler", state=State.GREEN)}
        new = {"scheduler": ComponentStatus(name="scheduler", state=State.RED, message="dead")}

        with patch(_EMIT_TARGET) as mock_emit:
            _dispatch_alerts(state, old, new)
            # Alert recorded in history, but emit_notification should be skipped (recent dedup)
            mock_emit.assert_not_called()

    def test_recent_warning_does_not_suppress_critical(self):
        """B-33: a warning cooldown must never mute the CRITICAL escalation."""
        state = HealthState()
        # A warning notification just fired for this component (old shared-key
        # behaviour would have started a cooldown that swallowed the RED alert).
        state.mark_notified("health_scheduler:warning")

        old = {"scheduler": ComponentStatus(name="scheduler", state=State.AMBER, message="slow")}
        new = {"scheduler": ComponentStatus(name="scheduler", state=State.RED, message="dead")}

        with patch(_EMIT_TARGET) as mock_emit:
            _dispatch_alerts(state, old, new)
            mock_emit.assert_called_once()
            assert mock_emit.call_args[0][0] == "health_critical"
            assert mock_emit.call_args[1]["severity"] == "critical"
            assert mock_emit.call_args[1]["dedupe_key"] == "health_scheduler:critical"

    def test_recent_recovery_does_not_suppress_critical(self):
        """B-33: a recovery→RED flap must still alert (recovery uses its own key)."""
        state = HealthState()
        old = {"scheduler": ComponentStatus(name="scheduler", state=State.RED)}
        new = {"scheduler": ComponentStatus(name="scheduler", state=State.GREEN, message="back")}
        with patch(_EMIT_TARGET):
            _dispatch_alerts(state, old, new)

        old = {"scheduler": ComponentStatus(name="scheduler", state=State.GREEN)}
        new = {"scheduler": ComponentStatus(name="scheduler", state=State.RED, message="dead again")}
        with patch(_EMIT_TARGET) as mock_emit:
            _dispatch_alerts(state, old, new)
            mock_emit.assert_called_once()
            assert mock_emit.call_args[0][0] == "health_critical"

    def test_suppressed_emission_does_not_arm_cooldown(self):
        """B-33: a DB-suppressed (or failed) emit must not start the in-monitor
        cooldown — otherwise one swallowed emission mutes the component."""
        state = HealthState()
        old = {"scheduler": ComponentStatus(name="scheduler", state=State.GREEN)}
        new = {"scheduler": ComponentStatus(name="scheduler", state=State.RED, message="dead")}

        with patch(_EMIT_TARGET, return_value={"status": "suppressed"}) as mock_emit:
            _dispatch_alerts(state, old, new)
            mock_emit.assert_called_once()
        assert not state.was_recently_alerted("health_scheduler:critical", cooldown_seconds=600)

        # The next RED transition retries the emission instead of being muted.
        with patch(_EMIT_TARGET, return_value={"status": "delivered"}) as mock_emit2:
            _dispatch_alerts(state, old, new)
            mock_emit2.assert_called_once()
        assert state.was_recently_alerted("health_scheduler:critical", cooldown_seconds=600)


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    def test_not_broken_initially(self):
        state = HealthState()
        assert not state.is_circuit_broken("bot:X")

    def test_broken_after_threshold(self):
        state = HealthState()
        for _ in range(HEALTH_CIRCUIT_BREAKER_COUNT):
            state.record_recovery("bot:X")
        assert state.is_circuit_broken("bot:X")

    def test_old_recoveries_pruned(self):
        state = HealthState()
        state._recovery_counts["bot:X"] = [
            time.monotonic() - HEALTH_CIRCUIT_BREAKER_WINDOW - 100
            for _ in range(5)
        ]
        assert not state.is_circuit_broken("bot:X")


# ---------------------------------------------------------------------------
# Check collectors (mocked)
# ---------------------------------------------------------------------------

class TestCheckScheduler:
    def test_green_with_enabled_jobs(self):
        jobs = [
            {"id": "job1", "next_run_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
             "interval_ms": 900_000, "enabled": True},
        ]
        with patch("forven.scheduler.get_enabled_jobs", return_value=jobs):
            with patch(
                "forven.db.kv_get",
                side_effect=lambda key, default=None: datetime.now(timezone.utc).isoformat()
                if key in {"scheduler:last_successful_tick", "scheduler:last_tick_started"}
                else "0",
            ):
                result = check_scheduler()
                assert result.state == State.GREEN

    def test_red_with_no_jobs(self):
        with patch("forven.scheduler.get_enabled_jobs", return_value=[]):
            result = check_scheduler()
            assert result.state == State.RED

    def test_amber_with_stale_jobs_but_live_heartbeat(self):
        jobs = [
            {"id": "stuck-job",
             "next_run_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
             "interval_ms": 900_000, "enabled": True},
        ]
        with patch("forven.scheduler.get_enabled_jobs", return_value=jobs):
            with patch(
                "forven.db.kv_get",
                side_effect=lambda key, default=None: datetime.now(timezone.utc).isoformat()
                if key in {"scheduler:last_successful_tick", "scheduler:last_tick_started"}
                else "0",
            ):
                result = check_scheduler()
                assert result.state == State.AMBER

    def test_red_with_stale_scheduler_heartbeat(self):
        jobs = [
            {"id": "job1", "next_run_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
             "interval_ms": 900_000, "enabled": True},
        ]
        stale_tick = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        with patch("forven.scheduler.get_enabled_jobs", return_value=jobs):
            with patch(
                "forven.db.kv_get",
                side_effect=lambda key, default=None: stale_tick
                if key in {"scheduler:last_successful_tick", "scheduler:last_tick_started"}
                else "0",
            ):
                # Neutralize the in-process tick fallback: any earlier test in
                # the same process that drove the scheduler (e.g. the circuit
                # breaker tests) leaves a fresh module-global _LAST_TICK_AT,
                # which check_scheduler prefers over the (stale) KV heartbeat.
                with patch("forven.scheduler.get_last_tick_at", return_value=None):
                    result = check_scheduler()
                assert result.state == State.RED

    def test_amber_with_consecutive_scheduler_errors(self):
        jobs = [
            {"id": "job1", "next_run_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
             "interval_ms": 900_000, "enabled": True},
        ]
        with patch("forven.scheduler.get_enabled_jobs", return_value=jobs):
            with patch(
                "forven.db.kv_get",
                side_effect=lambda key, default=None: (
                    datetime.now(timezone.utc).isoformat()
                    if key in {"scheduler:last_successful_tick", "scheduler:last_tick_started", "scheduler:last_error_at"}
                    else "6"
                ),
            ):
                result = check_scheduler()
                assert result.state == State.AMBER

    def test_scheduler_recovering_tick_ignores_stale_error_counter(self):
        jobs = [
            {"id": "job1", "next_run_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
             "interval_ms": 900_000, "enabled": True},
        ]
        now_iso = datetime.now(timezone.utc).isoformat()
        old_error = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        with patch("forven.scheduler.get_enabled_jobs", return_value=jobs):
            with patch(
                "forven.db.kv_get",
                side_effect=lambda key, default=None: (
                    now_iso if key == "scheduler:last_tick_started"
                    else old_error if key == "scheduler:last_error_at"
                    else old_error if key == "scheduler:last_successful_tick"
                    else "70"
                ),
            ):
                result = check_scheduler()
                assert result.state == State.AMBER
                assert "Recovering backlog" in result.message

    def test_scheduler_active_running_jobs_override_stale_tick_red(self):
        stale_tick = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()
        recent_running = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        jobs = [
            {
                "id": "forven-coding-daily",
                "next_run_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
                "running_since": recent_running,
                "interval_ms": 600_000,
                "enabled": True,
            }
        ]

        with patch("forven.scheduler.get_enabled_jobs", return_value=jobs):
            with patch(
                "forven.db.kv_get",
                side_effect=lambda key, default=None: (
                    stale_tick if key in {"scheduler:last_successful_tick", "scheduler:last_tick_started"}
                    else "0"
                ),
            ):
                result = check_scheduler()
                assert result.state == State.GREEN

    def test_scheduler_progress_heartbeat_keeps_scheduler_green(self):
        stale_tick = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()
        recent_progress = datetime.now(timezone.utc).isoformat()
        jobs = [
            {"id": "job1", "next_run_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
             "interval_ms": 900_000, "enabled": True},
        ]

        with patch("forven.scheduler.get_enabled_jobs", return_value=jobs):
            with patch(
                "forven.db.kv_get",
                side_effect=lambda key, default=None: (
                    recent_progress if key == "scheduler:last_progress_at"
                    else stale_tick if key in {"scheduler:last_successful_tick", "scheduler:last_tick_started"}
                    else "0"
                ),
            ):
                result = check_scheduler()
                assert result.state == State.GREEN


class TestCheckBots:
    def test_no_bots_is_green(self):
        with patch("forven.db.get_running_bots", return_value=[]):
            results = check_bots()
            assert len(results) == 1
            assert results[0].state == State.GREEN

    def test_stale_bot_heartbeat(self):
        bots = [{
            "bot_id": "b1", "name": "TestBot", "pid": 1234,
            "last_heartbeat": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
        }]
        with patch("forven.db.get_running_bots", return_value=bots):
            results = check_bots()
            assert len(results) == 1
            assert results[0].state == State.RED

    def test_fresh_bot_heartbeat(self):
        bots = [{
            "bot_id": "b1", "name": "TestBot", "pid": 1234,
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        }]
        with patch("forven.db.get_running_bots", return_value=bots):
            results = check_bots()
            assert len(results) == 1
            assert results[0].state == State.GREEN


class TestCheckLabWorker:
    def test_no_worker_is_green(self):
        with patch("forven.lab_db.get_lab_meta", return_value={}):
            result = check_lab_worker()
            assert result.state == State.GREEN

    def test_active_stale_worker(self):
        meta = {
            "state": "running", "pid": 999,
            "heartbeat_at": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
        }
        with patch("forven.lab_db.get_lab_meta", return_value=meta):
            result = check_lab_worker()
            assert result.state == State.RED

    def test_active_worker_with_epoch_heartbeat(self):
        meta = {
            "state": "running",
            "pid": 999,
            "heartbeat_at": time.time(),
        }
        with patch("forven.lab_db.get_lab_meta", return_value=meta):
            result = check_lab_worker()
            assert result.state == State.GREEN


class TestCheckBrainWorkers:
    def test_running_brain_job_is_not_marked_overdue(self):
        jobs = [
            {
                "id": "forven-testing-cycle",
                "next_run_at": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
                "running_since": datetime.now(timezone.utc).isoformat(),
                "interval_ms": 600_000,
                "enabled": True,
            }
        ]
        with patch("forven.scheduler.get_enabled_jobs", return_value=jobs):
            result = check_brain_workers()
            assert result.state == State.GREEN
            assert "running" in result.message


# ---------------------------------------------------------------------------
# Data integrity checks (mocked)
# ---------------------------------------------------------------------------

class TestCheckPipelineConsistency:
    def test_check_returns_results(self):
        """Pipeline check returns DataCheck list even on partial data."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_conn.execute.return_value.fetchone.return_value = {"cnt": 0}
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        with patch("forven.db.get_db", return_value=mock_conn):
            results = check_pipeline_consistency()
            assert len(results) >= 1

    def test_check_handles_exception(self):
        with patch("forven.db.get_db", side_effect=Exception("db locked")):
            results = check_pipeline_consistency()
            assert any(not r.passed for r in results)


class TestCheckSqliteHealth:
    def test_healthy_db(self, forven_db):
        result = check_sqlite_health()
        assert result.passed

    def test_handles_exception(self):
        with patch("forven.db.get_db", side_effect=Exception("locked")):
            result = check_sqlite_health()
            assert not result.passed
            assert result.severity == Severity.CRITICAL


# ---------------------------------------------------------------------------
# HealthMonitor lifecycle
# ---------------------------------------------------------------------------

class TestHealthMonitor:
    def test_start_stop(self):
        async def _run():
            monitor = HealthMonitor(poll_interval=0.1, data_check_interval=0.1)
            await monitor.start()
            assert monitor._running
            await asyncio.sleep(0.05)
            await monitor.stop()
            assert not monitor._running
        asyncio.run(_run())

    def test_poll_loop_survives_check_failure(self):
        """Individual check failures don't crash the loop."""
        async def _run():
            monitor = HealthMonitor(poll_interval=0.1, data_check_interval=100)
            with patch("forven.health_monitor.check_scheduler", side_effect=Exception("boom")):
                await monitor.start()
                await asyncio.sleep(0.2)
                await monitor.stop()
        asyncio.run(_run())

    def test_poll_loop_observes_and_alerts_in_manual_mode(self, forven_db):
        """In MANUAL mode the read-only checks AND alert dispatch must still run
        (so a down AI provider lights the critical banner / Discord alert), but
        the auto-recovery (which takes action) must NOT run."""
        async def _run():
            monitor = HealthMonitor(poll_interval=0.05, data_check_interval=100)
            down = ComponentStatus(name="ai_providers", state=State.RED, message="quota exhausted")

            with patch("forven.health_monitor.autonomous_runtime_allowed", return_value=False) as allowed, \
                 patch("forven.health_monitor.check_ai_providers", return_value=down) as ai_check, \
                 patch("forven.health_monitor._dispatch_alerts") as dispatch, \
                 patch("forven.health_monitor._attempt_recovery") as recovery:
                await monitor.start()
                try:
                    for _ in range(500):
                        if allowed.called:
                            break
                        await asyncio.sleep(0.01)
                    assert allowed.called, "monitor never completed its collection and alert pass"
                finally:
                    await monitor.stop()

            # Observability + alerting ran despite manual mode.
            assert ai_check.called
            assert dispatch.called
            # Auto-recovery did NOT run in manual mode.
            assert not recovery.called
            # And the RED state was actually recorded.
            assert monitor.state.get_component("ai_providers").state == State.RED
        asyncio.run(_run())

    def test_poll_loop_auto_recovers_in_autonomous_mode(self, forven_db):
        """In autonomous mode a freshly-RED component triggers auto-recovery."""
        async def _run():
            monitor = HealthMonitor(poll_interval=0.05, data_check_interval=100)
            down = ComponentStatus(name="ai_providers", state=State.RED, message="down")

            with patch("forven.health_monitor.autonomous_runtime_allowed", return_value=True), \
                 patch("forven.health_monitor.check_ai_providers", return_value=down), \
                 patch("forven.health_monitor._dispatch_alerts"), \
                 patch("forven.health_monitor._attempt_recovery") as recovery:
                await monitor.start()
                try:
                    for _ in range(500):
                        if recovery.called:
                            break
                        await asyncio.sleep(0.01)
                finally:
                    await monitor.stop()

            assert recovery.called
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Watchdog crash => visible (AMBER), never GREEN
# ---------------------------------------------------------------------------

class TestWatchdogCrashIsVisible:
    def test_data_freshness_crash_returns_amber_not_green(self):
        from forven.health_monitor import check_data_freshness

        with patch("forven.data_manager.data_manager_stats", side_effect=Exception("boom")):
            result = check_data_freshness()
        assert result.state == State.AMBER
        assert "Check failed" in result.message

    def test_pipeline_throughput_crash_returns_amber_not_green(self):
        with patch("forven.lab_db.get_lab_meta", side_effect=Exception("boom")):
            result = check_pipeline_throughput()
        assert result.state == State.AMBER
        assert "Check failed" in result.message


# ---------------------------------------------------------------------------
# Auto-recovery
# ---------------------------------------------------------------------------

class TestAutoRecovery:
    def test_recovery_records_attempt(self):
        from forven.health_monitor import _attempt_recovery

        async def _run():
            state = HealthState()
            status = ComponentStatus(name="scheduler", state=State.RED, message="dead")
            await _attempt_recovery(state, "scheduler", status)
            alerts = state.get_alerts()
            assert len(alerts) >= 1
            assert any("manual" in a.action_taken.lower() or "alert" in a.action_taken.lower()
                        for a in alerts)
        asyncio.run(_run())

    def test_circuit_breaker_escalates(self):
        from forven.health_monitor import _attempt_recovery

        async def _run():
            state = HealthState()
            for _ in range(HEALTH_CIRCUIT_BREAKER_COUNT):
                state.record_recovery("bot:TestBot")

            status = ComponentStatus(name="bot:TestBot", state=State.RED, message="dead")

            with patch(_EMIT_TARGET):
                await _attempt_recovery(state, "bot:TestBot", status)
                alerts = state.get_alerts()
                assert any("circuit breaker" in a.message.lower() for a in alerts)
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Pipeline throughput check
# ---------------------------------------------------------------------------

class TestPipelineThroughput:
    """Tests for the composite pipeline frozen detection."""

    def test_green_when_worker_stopped(self):
        with patch("forven.lab_db.get_lab_meta") as mock_meta:
            mock_meta.return_value = {"state": "stopped"}
            result = check_pipeline_throughput()
            assert result.state == State.GREEN

    def test_red_when_frozen(self):
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=PIPELINE_FROZEN_THRESHOLD_MINUTES + 10)).isoformat()
        worker_meta = {"state": "running"}
        progress_meta = {"last_job_completed_at": old_time}

        def _mock_meta(key, default=None):
            if key == "lab_worker_status":
                return worker_meta
            if key == "pipeline_progress":
                return progress_meta
            return default

        with patch("forven.lab_db.get_lab_meta", side_effect=_mock_meta):
            with patch("forven.lab_db.list_lab_jobs", return_value=[MagicMock()]):
                result = check_pipeline_throughput()
                assert result.state == State.RED
                assert "FROZEN" in result.message

    def test_green_when_recently_completed(self):
        recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        worker_meta = {"state": "running"}
        progress_meta = {"last_job_completed_at": recent}

        def _mock_meta(key, default=None):
            if key == "lab_worker_status":
                return worker_meta
            if key == "pipeline_progress":
                return progress_meta
            return default

        with patch("forven.lab_db.get_lab_meta", side_effect=_mock_meta):
            with patch("forven.lab_db.list_lab_jobs", return_value=[MagicMock()]):
                result = check_pipeline_throughput()
                assert result.state == State.GREEN


class TestPipelineThroughputRecovery:
    """Tests for pipeline_throughput recovery in _attempt_recovery."""

    def test_frozen_pipeline_triggers_kill(self):
        async def _run():
            state = HealthState()
            status = ComponentStatus(name="pipeline_throughput", state=State.RED, message="FROZEN")
            with patch(_EMIT_TARGET):
                with patch("forven.health_monitor._kill_lab_worker_processes", return_value=1) as mock_kill:
                    with patch("forven.lab_worker_service._reconcile_orchestrator_state"):
                        await _attempt_recovery(state, "pipeline_throughput", status)
                        mock_kill.assert_called_once()
        asyncio.run(_run())

