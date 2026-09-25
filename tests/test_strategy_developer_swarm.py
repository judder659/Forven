from __future__ import annotations


def test_assign_research_cycle_delegates_to_strategy_creation(monkeypatch, forven_db):
    from forven import brain
    import forven.strategy_creation as creation

    calls: list[bool] = []

    def _stub_creation_cycle():
        calls.append(True)
        return {"status": "queued", "task_id": 7}

    monkeypatch.setattr(creation, "run_creation_cycle", _stub_creation_cycle)

    result = brain.assign_research_cycle()

    assert result == {"status": "queued", "task_id": 7}
    assert calls == [True]
