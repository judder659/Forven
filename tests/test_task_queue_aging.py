from datetime import datetime, timedelta, timezone

import pytest

from forven import db, runtime_worker


@pytest.mark.usefixtures('forven_db')
def test_aged_work_runs_before_new_high_priority_work_but_after_user_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('forven.system_mode_policy.is_manual_mode', lambda: False)
    monkeypatch.setattr(db, '_claim_ownership_for_task', lambda *args: ('ok', None))
    now = datetime.now(timezone.utc)
    with db.get_db() as conn:
        for task_id, minutes, source, priority, retry in [
            (91001, 60, 'system', 1, None), (91002, 1, 'system', 4, None),
            (91003, 0, 'user', 0, None), (91004, 90, 'system', 4, (now+timedelta(hours=1)).isoformat()),
        ]:
            conn.execute("INSERT INTO agent_tasks (id,agent_id,type,title,status,priority,source,created_at,retry_at) VALUES (?, 'tester','research','probe','pending',?,?,?,?)",
                         (task_id,priority,source,(now-timedelta(minutes=minutes)).isoformat(),retry))
    claimed = db.claim_pending_agent_tasks('tester', limit=10)
    assert [r['id'] for r in claimed] == [91003, 91001, 91002]


@pytest.mark.usefixtures('forven_db')
def test_research_admitted_after_long_wait_is_not_preempted_again() -> None:
    now = datetime.now(timezone.utc)
    with db.get_db() as conn:
        conn.execute("INSERT INTO agent_tasks (id,agent_id,type,title,status,priority,source,created_at,started_at,input_data) VALUES (91001,'strategy-developer','research','probe','running',1,'system',?,?,'{}')",
                     ((now-timedelta(minutes=60)).isoformat(),(now-timedelta(minutes=5)).isoformat()))
        conn.execute("INSERT INTO agent_tasks (id,agent_id,type,title,status,priority,source,created_at) VALUES (91002,'strategy-developer','develop_candidate','probe','pending',4,'system',?)", (now.isoformat(),))
    assert runtime_worker._preempt_research_for_waiting_develop_candidate_tasks() == set()
    with db.get_db() as conn:
        assert conn.execute('SELECT status FROM agent_tasks WHERE id=91001').fetchone()[0] == 'running'


@pytest.mark.usefixtures('forven_db')
def test_queue_health_distinguishes_retry_backoff_from_stuck_work() -> None:
    from forven.control_plane.status import health_check

    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=2)).isoformat()
    future = (now + timedelta(hours=1)).isoformat()
    with db.get_db() as conn:
        for task_id, status, retry in [(91001, 'pending', None), (91002, 'pending', future),
                                       (91003, 'done', None), (91004, 'failed', None)]:
            conn.execute("INSERT INTO agent_tasks (id,agent_id,type,title,status,created_at,retry_at) VALUES (?,'tester','research','probe',?,?,?)",
                         (task_id,status,old,retry))
    queues = health_check()['details']['queues']
    assert queues['agent_pending'] == 2
    assert queues['agent_running'] == 0
    assert queues['agent_stale_pending'] == 1


@pytest.mark.usefixtures('forven_db')
def test_health_reports_only_enabled_overdue_jobs_without_a_running_owner() -> None:
    from forven.control_plane.status import health_check

    now = datetime.now(timezone.utc)
    old = (now - timedelta(minutes=10)).astimezone(timezone(timedelta(hours=-3))).isoformat()
    future = (now + timedelta(minutes=10)).isoformat()
    with db.get_db() as conn:
        conn.execute('DELETE FROM scheduler_jobs')
        for job_id, enabled, due, running in [
            ('overdue', 1, old, None), ('running', 1, old, now.isoformat()),
            ('future', 1, future, None), ('disabled', 0, old, None),
            ('unknown', 1, None, None), ('invalid', 1, 'invalid', None),
        ]:
            conn.execute(
                "INSERT INTO scheduler_jobs (id,name,enabled,schedule_type,schedule_expr,command,next_run_at,running_since) VALUES (?,? ,?,'interval','300','test',?,?)",
                (job_id, job_id, enabled, due, running),
            )
    detail = health_check()['details']
    assert detail['overdue_due_scheduler_jobs'] == 1
    assert detail['overdue_due_scheduler_job_ids'] == ['overdue']
