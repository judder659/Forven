"""A sandbox intake commits the candidate/task link without parent code loading."""
import json

import pytest

from forven.db import get_db


@pytest.mark.parametrize('matching_hypothesis', [True, False])
def test_intake_links_only_its_running_candidate_task(forven_db, monkeypatch, tmp_path, matching_hypothesis):
    from forven.strategies import imported, registry
    from forven.strategies.intake import register_imported_strategy_file

    monkeypatch.setattr(imported, '__file__', str(tmp_path / '__init__.py'))
    (tmp_path / 'registration_handoff_fixture.py').write_text('TYPE_NAME = "handoff_fixture"\n')
    monkeypatch.setattr('forven.sandbox.strategy_worker._reset_worker', lambda: None)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO hypotheses(id,display_id,title,market_thesis,mechanism,target_assets,target_timeframes,lane,source_type) "
            "VALUES ('h-intake','H-intake','Test','Test','Test','[\"BTC/USDT\"]','[\"1h\"]','test','test')"
        )
        conn.execute(
            "INSERT INTO agent_tasks(agent_id,type,title,status,display_id,input_data) "
            "VALUES ('strategy-developer','develop_candidate','Test','running','T-intake',?)",
            (json.dumps({'hypothesis_id': 'h-intake' if matching_hypothesis else 'another-hypothesis'}),),
        )
    result = register_imported_strategy_file(
        module_name='registration_handoff_fixture', source='agent_register',
        _hypothesis_id='h-intake', _origin_task_id='T-intake',
        _validated_meta={
            'ok': True, 'type_name': 'handoff_fixture', 'asset': 'BTC',
            'certified': True, 'canonical_params': {}, 'lookahead_verifiable': True,
        },
    )
    assert result['sandbox_only']
    assert result['runtime_type'].startswith('imported__')
    assert 'handoff_fixture' not in registry._TYPE_MAP
    with get_db() as conn:
        link = conn.execute("SELECT strategy_id FROM agent_tasks WHERE display_id='T-intake'").fetchone()[0]
    assert link == (result['strategy_id'] if matching_hypothesis else None)
