import pytest

from scripts.runtime_endurance import assess, summarize


def healthy() -> dict:
    return {
        "status": "ok", "issues": [],
        "details": {
            "runtime_threads": [{"name": name, "alive": True} for name in (
                "forven-scheduler-loop", "forven-headless-agent-loop", "forven-headless-brain-loop",
            )],
            "scheduler_age_seconds": 5,
            "api_task_worker": {"pid": 123, "loops": {"agent": {"fresh": True}, "brain": {"fresh": True}}},
            "event_loop": {"recent_stalls_over_ws_risk": 0},
        },
    }


def test_recovery_does_not_erase_a_failed_request() -> None:
    report = summarize([
        {"at": "first", "violations": ["request_failed"]},
        {"at": "second", "health": healthy(), "latency_seconds": .1, "violations": []},
    ])
    assert report['ok'] is False
    assert report['violations'] == {'request_failed': 1}
    assert report['samples'] == 2
    assert report['complete'] is False


def test_restart_is_not_mistaken_for_continuous_availability() -> None:
    before, after = healthy(), healthy()
    after['details']['api_task_worker']['pid'] = 456
    report = summarize([
        {'at': str(i), 'health': state, 'latency_seconds': .1, 'violations': []}
        for i, state in enumerate((before, after))
    ])
    assert report['ok'] is False
    assert report['pids'] == [123, 456]


@pytest.mark.parametrize('missing', ['runtime_threads', 'scheduler_age_seconds', 'api_task_worker', 'event_loop'])
def test_missing_monitoring_is_not_a_pass(missing: str) -> None:
    state = healthy()
    del state['details'][missing]
    assert assess(state, .1)


def test_slow_or_inconsistent_health_is_not_a_pass() -> None:
    state = healthy()
    assert assess(state, .1) == []
    assert 'request_over_5_seconds' in assess(state, 6)
    state['issues'] = ['runtime stopped']
    assert 'health:runtime stopped' in assess(state, .1)


def test_empty_run_has_no_success() -> None:
    assert summarize([])['ok'] is False
