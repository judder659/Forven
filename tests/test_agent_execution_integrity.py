"""Real task rows, fake providers: no replay, honest stops, durable usage."""
import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from forven.agents import runner
from forven.agents.execution_state import current_execution, load_execution, record_response
from forven.agents.providers import ProviderResponse, ToolCall
from forven.db import get_db


class Provider:
    def __init__(self, fail_second=False):
        self.calls = 0
        self.fail_second = fail_second
        self.saw_saved_result = False

    async def call(self, model_id, messages, system, tools, token):
        self.calls += 1
        if self.fail_second and self.calls == 2:
            raise httpx.ReadTimeout("provider interrupted")
        self.saw_saved_result |= any(m.get("role") == "tool" for m in messages)
        return ProviderResponse(
            text="working" if self.calls == 1 else "finished",
            tool_calls=[ToolCall(id="one", name="artifact", input={})] if self.calls == 1 else [],
            stop=self.calls != 1, raw_assistant_message={"role": "assistant", "content": "step"},
            usage={"input_tokens": 100, "output_tokens": 10},
        )

    def append_assistant(self, messages, response):
        messages.append(response.raw_assistant_message)

    def append_tool_results(self, messages, results):
        messages.extend({"role": "tool", "tool_call_id": key, "content": result} for key, result in results)


@pytest.fixture
def context(forven_db, monkeypatch):
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO agents(id,name,role,created_at) VALUES ('quant-researcher','Research','researcher',?)", (datetime.now(timezone.utc).isoformat(),))
        cursor = conn.execute("INSERT INTO agent_tasks(agent_id,type,title,description,status,created_at,display_id) VALUES ('quant-researcher','analysis','Check','Check','pending',?,'T88881')", (datetime.now(timezone.utc).isoformat(),))
        task = dict(conn.execute("SELECT * FROM agent_tasks WHERE id=?", (cursor.lastrowid,)).fetchone())
    monkeypatch.setattr('forven.auth.store.get_token', lambda *a: 'fake')
    monkeypatch.setattr('forven.model_selection.assert_callable', lambda *a, **k: None)
    monkeypatch.setattr(runner, '_provider_has_credentials', lambda *a: True)
    monkeypatch.setattr(runner, '_resolve_tool_call_chain', lambda *a: [('openai','gpt-5.2')])
    monkeypatch.setattr(runner, '_resolve_backup_provider', lambda *a: None)
    monkeypatch.setattr(runner, '_check_task_owner', lambda *a, **k: (None,True))
    monkeypatch.setattr(runner, 'read_workspace', lambda *a, **k: '')
    monkeypatch.setattr(runner, 'append_workspace', lambda *a, **k: None)
    monkeypatch.setattr(runner, 'build_agent_context', lambda *a, **k: 'system')
    monkeypatch.setattr(runner, '_get_tools_for_agent', lambda *a, **k: [{'name':'artifact','description':'Produce an artifact','input_schema':{}}])
    monkeypatch.setattr(runner, '_should_queue_brain_callback_for_completed_task', lambda **k: False)
    agent = {'id':'quant-researcher','name':'Research','model':'openai','model_id':'gpt-5.2'}
    return agent, task


def status(task):
    with get_db() as conn:
        return dict(conn.execute('SELECT * FROM agent_tasks WHERE id=?', (task['id'],)).fetchone())


@pytest.mark.parametrize('candidate_type', ['develop_candidate', 'generate_strategies'])
def test_candidate_data_block_can_recheck_without_model_or_tool_calls(context, monkeypatch, candidate_type):
    import json
    from fastapi import HTTPException
    from forven.agents.execution_state import resume_checkpoint

    agent, task = context
    with get_db() as conn:
        conn.execute("INSERT INTO hypotheses(id,title,market_thesis,mechanism,target_assets,target_timeframes,lane,source_type,status,manager_state) VALUES ('h-preflight','Test','Test','Funding','[\"BTC/USDT\"]','[\"1h\"]','test','test','researching','active')")
        conn.execute("UPDATE agent_tasks SET type=?,input_data=? WHERE id=?", (candidate_type, json.dumps({'hypothesis_id': 'h-preflight'}), task['id']))
    task = status(task)
    checks = []
    def preflight(*args):
        checks.append(args)
        return {'can_generate': False, 'issues': ['Missing funding'], 'warnings': []}
    monkeypatch.setattr('forven.strategies.idea_readiness.candidate_readiness', preflight)
    async def forbidden(*args, **kwargs):
        pytest.fail('Blocked input preflight must not call a provider')
    monkeypatch.setattr(runner, '_call_with_tools', forbidden)
    asyncio.run(runner.run_agent_task(agent, task))
    assert status(task)['status'] == 'blocked'
    assert 'Missing funding' in status(task)['error']
    assert load_execution(task['id'], agent['id']).checkpoint['data_preflight']
    with pytest.raises(HTTPException, match='inputs are still blocked'):
        resume_checkpoint(task['id'])
    assert len(checks) == 2
    assert status(task)['status'] == 'blocked'
    monkeypatch.setattr('forven.strategies.idea_readiness.candidate_readiness', lambda *a: {
        'can_generate': True, 'issues': [], 'warnings': [],
    })
    called = []
    async def after_repair(*args, **kwargs):
        assert not load_execution(task['id'], agent['id']).checkpoint.get('data_preflight')
        called.append(True)
        return 'No candidate registered', {}
    monkeypatch.setattr(runner, '_call_with_tools', after_repair)
    resume_checkpoint(task['id'])
    asyncio.run(runner.run_agent_task(agent, status(task)))
    assert called == [True]
    assert 'without a registered strategy' in status(task)['error']


def test_provider_retry_resumes_after_tool_without_replay(context, monkeypatch):
    agent, task = context
    provider = Provider(fail_second=True)
    monkeypatch.setattr('forven.agents.providers.get_provider', lambda *a: provider)
    calls = []
    async def tool(*a):
        calls.append(a)
        return 'created artifact'
    monkeypatch.setattr(runner, '_execute_tool', tool)
    asyncio.run(runner.run_agent_task(agent, task))
    assert status(task)['status'] == 'pending'
    assert load_execution(task['id'], agent['id']).checkpoint['messages'][-1]['role'] == 'tool'
    asyncio.run(runner.run_agent_task(agent, status(task)))
    assert status(task)['status'] == 'done'
    assert len(calls) == 1
    assert provider.saw_saved_result
    with get_db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM agent_model_calls WHERE task_id=?',(task['id'],)).fetchone()[0] == 2
        assert conn.execute('SELECT input_tokens FROM agent_spend_daily WHERE agent_id=?',(agent['id'],)).fetchone()[0] == 200


def test_interrupted_tool_is_blocked_and_cannot_be_replayed(context, monkeypatch):
    agent, task = context
    monkeypatch.setattr('forven.agents.providers.get_provider', lambda *a: Provider())
    calls = []
    async def tool(*a):
        calls.append(a)
        raise httpx.ReadTimeout('uncertain side effect')
    monkeypatch.setattr(runner, '_execute_tool', tool)
    asyncio.run(runner.run_agent_task(agent, task))
    assert status(task)['status'] == 'blocked'
    asyncio.run(runner.run_agent_task(agent, status(task)))
    assert len(calls) == 1
    assert status(task)['input_tokens'] == 100


def test_tool_limit_is_incomplete_without_extra_model_call(context, monkeypatch):
    agent, task = context
    provider = Provider()
    monkeypatch.setattr('forven.agents.providers.get_provider', lambda *a: provider)
    monkeypatch.setattr(runner, 'MAX_TOOL_ROUNDS', 1)
    async def tool(*a): return 'recorded'
    monkeypatch.setattr(runner, '_execute_tool', tool)
    result = asyncio.run(runner.run_agent_task(agent, task))
    assert result['completion_state'] == 'incomplete'
    assert status(task)['status'] == 'blocked'
    assert provider.calls == 1


def test_usage_is_charged_during_task_and_not_double_counted(context, monkeypatch):
    from forven.billing_guard import get_spend_today, get_unpriced_spend_today
    agent, task = context
    token = current_execution.set(load_execution(task['id'], agent['id']))
    monkeypatch.setattr('forven.cost_pricing.has_pricing', lambda *a: True)
    monkeypatch.setattr('forven.cost_pricing.estimate_cost_usd', lambda *a: 2.0)
    try:
        record_response('provider-a','actual-model',{'input_tokens':100,'output_tokens':20})
        assert get_spend_today() == 2.0
        with get_db() as conn:
            conn.execute('UPDATE agent_tasks SET cost_usd=2 WHERE id=?',(task['id'],))
        assert get_spend_today() == 2.0
        monkeypatch.setattr('forven.cost_pricing.has_pricing', lambda *a: False)
        record_response('unknown','unpriced',{'input_tokens':1000000,'output_tokens':0})
        assert get_unpriced_spend_today() > 0
    finally:
        current_execution.reset(token)

def test_safe_checkpoint_can_resume_but_uncertain_tool_cannot(context):
    from fastapi import HTTPException
    from forven.agents.execution_state import resume_checkpoint
    agent, task = context
    state = load_execution(task['id'], agent['id'])
    state.save(messages=[{'role':'tool','content':'saved'}], provider='openai', model_id='gpt-5.2', inflight=False)
    with get_db() as conn:
        conn.execute("UPDATE agent_tasks SET status='blocked' WHERE id=?", (task['id'],))
    assert resume_checkpoint(task['id'])['status'] == 'pending'
    state.save(inflight=True)
    with get_db() as conn:
        conn.execute("UPDATE agent_tasks SET status='blocked' WHERE id=?", (task['id'],))
    with pytest.raises(HTTPException) as error:
        resume_checkpoint(task['id'])
    assert error.value.status_code == 409
    assert status(task)['status'] == 'blocked'


def test_failed_handoff_stays_blocked_and_resumes_without_model_replay(context, monkeypatch):
    from forven import brain
    agent, task = context
    task['strategy_id'] = 'S_HANDOFF'
    with get_db() as conn:
        conn.execute("INSERT INTO strategies(id,name,type,symbol,timeframe,stage,status,params,created_at) VALUES ('S_HANDOFF','Test','rsi_momentum','BTC','1h','quick_screen','quick_screen','{}',?)",(datetime.now(timezone.utc).isoformat(),))
        conn.execute("UPDATE agent_tasks SET strategy_id='S_HANDOFF' WHERE id=?",(task['id'],))
    monkeypatch.setattr(brain,'STAGE_TO_AGENT',{'quick_screen':agent['id'],'gauntlet':'simulation-agent'})
    monkeypatch.setattr(brain,'NEXT_STAGE',{'quick_screen':'gauntlet'})
    monkeypatch.setattr(brain,'transition_stage',lambda *a,**k: {'to':'gauntlet'})
    monkeypatch.setattr(runner,'PIPELINE_AUTO_HANDOFF_TASK_TYPES',{'quick_screen':{'analysis'}})
    provider=Provider()
    monkeypatch.setattr('forven.agents.providers.get_provider',lambda *a:provider)
    async def tool(*a): return 'done'
    monkeypatch.setattr(runner,'_execute_tool',tool)
    def fail(*a,**k): raise RuntimeError('handoff unavailable')
    monkeypatch.setattr('forven.db.handoff_task',fail)
    asyncio.run(runner.run_agent_task(agent,task))
    assert status(task)['status']=='blocked'
    assert load_execution(task['id'],agent['id']).checkpoint['pending_handoff']=='simulation-agent'
    calls=provider.calls
    monkeypatch.setattr('forven.db.handoff_task',lambda *a,**k: None)
    result=asyncio.run(runner.run_agent_task(agent,status(task)))
    assert result['handoff_to']=='simulation-agent'
    assert provider.calls==calls

@pytest.mark.parametrize('candidate_type', ['develop_candidate', 'generate_strategies'])
def test_candidate_task_requires_registered_strategy_evidence(context, monkeypatch, candidate_type):
    agent, task = context
    task['type']=candidate_type
    monkeypatch.setattr('forven.strategies.idea_readiness.candidate_readiness', lambda *a: {'can_generate':True,'issues':[]})
    provider=Provider()
    monkeypatch.setattr('forven.agents.providers.get_provider',lambda *a:provider)
    async def tool(*a): return 'I created a strategy'
    monkeypatch.setattr(runner,'_execute_tool',tool)
    result=asyncio.run(runner.run_agent_task(agent,task))
    assert status(task)['status']=='blocked'
    assert 'without a registered strategy' in result['reason']


def test_usage_prices_actual_models_and_keeps_free_distinct_from_unpriced(context, monkeypatch):
    from forven.agents.execution_state import task_usage
    from forven.billing_guard import get_unpriced_spend_today
    agent, task=context
    token=current_execution.set(load_execution(task['id'],agent['id']))
    monkeypatch.setattr('forven.cost_pricing.has_pricing',lambda *a: True)
    monkeypatch.setattr('forven.cost_pricing.estimate_cost_usd',lambda provider,*a: 2.0 if provider=='first' else 0.0)
    try:
        record_response('first','primary',{'input_tokens':50,'output_tokens':5})
        record_response('free-local','actual-fallback',{'input_tokens':80,'output_tokens':8})
        usage=task_usage(task['id'])
        assert usage['provider']=='free-local'
        assert usage['model_id']=='actual-fallback'
        assert usage['cost_usd']==2.0
        assert usage['input_tokens']==130
        assert get_unpriced_spend_today()==0
    finally:
        current_execution.reset(token)
