import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from self_improving_ai.agents.portfolio import learning, application
from self_improving_ai.dashboard.app import app


@pytest.fixture
def api(tmp_path,monkeypatch):
    monkeypatch.setenv('SIA_PORTFOLIO_LEARNING_DIR',str(tmp_path))
    monkeypatch.delenv('SIA_PORTFOLIO_LEARNER_FACTORY',raising=False)
    return TestClient(app)


def calculate(api,event='event1',target=None):
    payload={'task':'allocation','client_id':'CLI-001','observation':{
        'event_id':event,'evidence_group_id':'analysis1',
        'parameter_sources':{'target_stock_weight':'manual'} if target is not None else {}}}
    if target is not None: payload['target_stock_weight']=target
    return api.post('/api/portfolio/execute',json=payload)


def test_default_manual_source_actual_baseline_and_no_fake_learning(api):
    a=calculate(api).json()
    b=calculate(api,'event2',.6).json()
    assert a['result']==b['result']|{'target_source':'stored_draft'}
    view=api.get('/api/portfolio/learning/CLI-001').json()
    assert view['status']=='waiting_for_module' and view['context'] is None
    assert view['pending_count']==2
    rows=[r['event'] for r in view['events'] if r['revision']==2]
    assert rows[0]['parameter_sources']['target_stock_weight']=='default'
    assert rows[1]['parameter_sources']['target_stock_weight']=='manual'
    assert rows[0]['prior_context']['current_stock_weight']==pytest.approx(.7986371379897785)
    assert rows[0]['evidence_group_id']==rows[1]['evidence_group_id']
    assert rows[0]['outcome']=='success'  # never acceptance/execution


def test_retries_conflicts_isolation_and_persistence(api):
    first=calculate(api).json()
    assert calculate(api).json()['run_id']==first['run_id']
    assert calculate(api,target=.5).status_code==409
    assert len(learning.Journal().rows('CLI-001'))==2
    assert not learning.Journal().rows('CLI-002')
    assert api.post('/api/portfolio/learning/CLI-002/retract/event1').status_code==404
    assert api.post('/api/portfolio/learning/CLI-001/retract/event1').status_code==200
    api.post('/api/portfolio/learning/CLI-001/retract/event1')
    assert len(learning.Journal().rows('CLI-001'))==3
    exported=api.get('/api/portfolio/learning/CLI-001/export').json()
    assert exported['deliveries'][-1]['kind']=='retract'


def test_legacy_missing_client_does_not_learn_and_unknown_rejected(api):
    result=api.post('/api/portfolio/execute',json={'task':'allocation'}).json()
    assert result['learning']['status']=='client_not_selected'
    assert learning.Journal().rows('CLI-001')==[]
    assert api.post('/api/portfolio/execute',json={'task':'allocation','client_id':'unknown'}).status_code==409
    assert api.post('/api/portfolio/agent',json={'client_id':'CLI-001','message':'Review CLI-002'}).status_code==409
    assert api.post('/api/portfolio/learning/CLI-001/notes',json={'message':'Thompson Trust is conservative'}).status_code==409
    assert learning.Journal().rows('CLI-001')==[]


class ContractDouble:
    """Transport contract double only; not a preference inference implementation."""
    def __init__(self): self.events=[];self.fail=False
    def process_event(self,event):
        if self.fail: raise RuntimeError('private details must not leak')
        self.events.append(event)
        return {'status':'ok','changes':[]}
    def get_context(self,client,scope):
        return {'client_id':client,'declared':{},'beliefs':{},'attention':{},'test_only':True}
    def retract_event(self,client,event):
        if self.fail: return {'status':'error'}
        self.events.append({'retracted':event})
        return {'status':'ok'}


def test_adapter_replay_failure_and_stale_context_suppression(api,monkeypatch):
    calculate(api)
    adapter=ContractDouble()
    monkeypatch.setattr(learning,'adapter_for',lambda *_:adapter)
    assert api.post('/api/portfolio/learning/CLI-001/retry').json()['pending_count']==0
    assert len(adapter.events)==1
    api.post('/api/portfolio/learning/CLI-001/retry')
    assert len(adapter.events)==1
    adapter.fail=True
    result=api.post('/api/portfolio/learning/CLI-001/retract/event1').json()
    assert result['context'] is None and result['pending_count']==1
    adapter.fail=False
    result=api.post('/api/portfolio/learning/CLI-001/retry').json()
    assert result['context']['client_id']=='CLI-001'
    assert adapter.events[-1]=={'retracted':'event1'}


def test_cross_client_context_and_storage_failure_are_not_used(api,monkeypatch):
    class WrongClient(ContractDouble):
        def get_context(self,client,scope): return {'client_id':'CLI-002','declared':{'risk':'wrong'}}
    monkeypatch.setattr(learning,'adapter_for',lambda *_:WrongClient())
    assert learning.get_client_learning_context('CLI-001')['context'] is None
    def unavailable(*args,**kwargs): raise OSError('private database path')
    monkeypatch.setattr(learning,'Journal',unavailable)
    result=calculate(api).json()
    assert result['status']=='success'
    assert result['learning']['status']=='storage_error'
    assert 'private database path' not in json.dumps(result)
    note=api.post('/api/portfolio/learning/CLI-001/notes',json={'message':'test note'}).json()
    assert note['status']=='error'


def test_default_projection_controls_are_not_user_requests(api):
    api.post('/api/portfolio/execute',json={'client_id':'CLI-001','task':'projection'})
    event=json.loads(learning.Journal().rows('CLI-001')[0]['payload'])
    assert 'num_simulations' not in event['requested_parameters']
    assert event['effective_parameters']['num_simulations']==5000
    assert event['parameter_sources']['num_simulations']=='default'


def test_concurrent_duplicate_journal_append(api):
    event=learning.event_from('CLI-001','allocation',{}, {'event_id':'parallel'})
    journal=learning.Journal()
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _:journal.append(event),range(12)))
    assert len(journal.rows('CLI-001'))==1


def test_late_completion_cannot_resurrect_retracted_event(api,monkeypatch):
    event=learning.event_from('CLI-001','advisor_request',{}, {'event_id':'slow-request'})
    journal,_=learning.record_start(event)
    journal.retract('CLI-001','slow-request')
    adapter=ContractDouble()
    monkeypatch.setattr(learning,'adapter_for',lambda *_:adapter)
    learning.record_finish(journal,event,{'status':'success','run_id':'late','trace':[]})
    assert len(adapter.events)==1
    assert adapter.events[-1]=={'retracted':'slow-request'}
    assert journal.rows('CLI-001')[-1]['status']=='superseded'


@pytest.mark.asyncio
async def test_context_is_actually_injected_into_adk_instruction():
    from tests.test_agents.test_portfolio_agent import ScriptedModel
    from self_improving_ai.agents.portfolio.service import run_portfolio_agent
    class InspectingModel(ScriptedModel):
        async def generate_content_async(self,llm_request,stream=False):
            assert 'Selected client ID: CLI-001' in llm_request.config.system_instruction
            assert 'contract-context-marker' in llm_request.config.system_instruction
            async for response in super().generate_content_async(llm_request,stream): yield response
    result=await run_portfolio_agent('holdings',client_id='CLI-001',model=InspectingModel(),
        learning_context={'client_id':'CLI-001','context':{'summary':'contract-context-marker'}})
    assert result['status']=='success'


def test_bound_agent_receives_note_context_and_preserves_raw_origin(api,monkeypatch):
    seen={}
    async def fake(message,**kwargs):
        seen.update(kwargs)
        return {'run_id':'transport-test','status':'success','response':'test only','trace':[]}
    monkeypatch.setattr(application,'run_portfolio_agent',fake)
    result=api.post('/api/portfolio/agent',json={'client_id':'CLI-002','message':'假设客户保守',
        'observation':{'event_id':'agent1','text_source':'manual'}}).json()
    assert seen['client_id']=='CLI-002'
    assert seen['learning_context']['context'] is None
    event=learning.Journal().rows('CLI-002')[0]
    assert json.loads(event['payload'])['user_text']=='假设客户保守'
    assert result['learning_context_used']['client_id']=='CLI-002'
    assert learning.Journal().rows('CLI-001')==[]


@pytest.mark.asyncio
async def test_bound_adk_rejects_other_clients():
    from tests.test_agents.test_portfolio_agent import ScriptedModel
    from self_improving_ai.agents.portfolio.service import run_portfolio_agent
    result=await run_portfolio_agent('holdings',client_id='CLI-001',model=ScriptedModel(
        tool_name='get_holdings',arguments={'client_id':'CLI-002'}))
    assert 'CLIENT_CONTEXT_MISMATCH' in result['response']


def test_failed_initial_revision_can_be_replaced_by_completed_input(api,monkeypatch):
    class NeedsResult(ContractDouble):
        def process_event(self,event):
            self.events.append(event)
            return {'status':'partial' if event['revision']==1 else 'ok'}
    adapter=NeedsResult()
    monkeypatch.setattr(learning,'adapter_for',lambda *_:adapter)
    event=learning.event_from('CLI-001','advisor_request',{}, {'event_id':'needs-result'})
    journal,_=learning.record_start(event)
    assert journal.sync('CLI-001')=='processing_pending'
    result=learning.record_finish(journal,event,{'status':'success','run_id':'finished'})
    assert result['learning']['status']=='connected'
    assert [x['revision'] for x in adapter.events]==[1,2]
    assert journal.rows('CLI-001')[0]['status']=='superseded'


def test_bad_event_does_not_block_its_retraction_or_later_events(api,monkeypatch):
    class RejectOne(ContractDouble):
        def process_event(self,event):
            if event['event_id']=='bad-event': return {'status':'partial'}
            return super().process_event(event)
    adapter=RejectOne()
    monkeypatch.setattr(learning,'adapter_for',lambda *_:adapter)
    calculate(api,'bad-event')
    calculate(api,'good-event')
    assert not adapter.events
    reply=api.post('/api/portfolio/learning/CLI-001/retract/bad-event').json()
    assert reply['pending_count']==0
    assert adapter.events[0]=={'retracted':'bad-event'}
    assert adapter.events[1]['event_id']=='good-event'


def test_retraction_precedes_unrelated_poison_record(api,monkeypatch):
    adapter=ContractDouble()
    monkeypatch.setattr(learning,'adapter_for',lambda *_:adapter)
    calculate(api,'previously-applied')
    adapter.fail=True
    calculate(api,'bad-event')
    adapter.fail=False
    original=adapter.process_event
    adapter.process_event=lambda event:{'status':'partial'} if event['event_id']=='bad-event' else original(event)
    result=api.post('/api/portfolio/learning/CLI-001/retract/previously-applied').json()
    assert adapter.events[-1]=={'retracted':'previously-applied'}
    assert result['context'] is None


def test_cached_request_refreshes_delivery_status_without_recalculation(api,monkeypatch):
    first=calculate(api).json()
    adapter=ContractDouble()
    monkeypatch.setattr(learning,'adapter_for',lambda *_:adapter)
    second=calculate(api).json()
    assert first['run_id']==second['run_id']
    assert second['learning']['status']=='connected'
    assert len(adapter.events)==1


@pytest.mark.parametrize('bad_context',[
    {'client_id':'CLI-001','summary':{'invalid':'object'}},
    {'client_id':'CLI-001','unknowns':[None]},
    {'client_id':'CLI-001','team_declared':'not-a-list'},
    {'client_id':'CLI-001','scope':{'account_id':'ACC-001A'}},
    {'client_id':'CLI-001','status':'error'},
])
def test_invalid_or_wrong_scope_context_never_enters_agent(api,monkeypatch,bad_context):
    adapter=ContractDouble()
    adapter.get_context=lambda *_:bad_context
    monkeypatch.setattr(learning,'adapter_for',lambda *_:adapter)
    assert learning.get_client_learning_context('CLI-001')['context'] is None


def test_wrong_client_acknowledgement_does_not_confirm_delivery(api,monkeypatch):
    adapter=ContractDouble()
    adapter.process_event=lambda event:{'status':'ok','state':{'client_id':'CLI-002'}}
    monkeypatch.setattr(learning,'adapter_for',lambda *_:adapter)
    result=calculate(api).json()
    assert result['learning']['status']=='module_error'
    assert learning.Journal().view('CLI-001')['pending_count']==1


def test_slow_adapter_does_not_block_calculation_or_allow_concurrent_module_calls(api,monkeypatch):
    release=Event()
    class Slow(ContractDouble):
        def process_event(self,event):
            assert release.wait(5)
            return super().process_event(event)
    adapter=Slow()
    monkeypatch.setattr(learning,'adapter_for',lambda *_:adapter)
    original=learning.bounded_adapter_call
    # Shorter transport deadline keeps this fault-injection test fast.
    monkeypatch.setattr(learning,'bounded_adapter_call',lambda fn:original(fn,timeout=.1))
    try:
        started=monotonic()
        result=calculate(api).json()
        assert result['status']=='success' and result['learning']['status']=='module_busy'
        assert monotonic()-started<1.5
        view=learning.Journal().view('CLI-001')
        assert view['status']=='module_busy' and view['context'] is None
        assert api.post('/api/portfolio/learning/CLI-001/retract/event1').status_code==200
        assert not adapter.events
    finally:
        release.set()
        # Wait for the external call to leave the gate before another test runs.
        deadline=monotonic()+2
        while learning._adapter_gate.locked() and monotonic()<deadline: sleep(.01)
    assert learning.Journal().sync('CLI-001')=='connected'
    assert adapter.events[-1]=={'retracted':'event1'}


@pytest.mark.asyncio
async def test_parallel_agent_retry_does_not_launch_another_model_call(api,monkeypatch):
    import asyncio
    started,release=asyncio.Event(),asyncio.Event()
    calls=[]
    async def fake(message,**kwargs):
        calls.append(message);started.set();await release.wait()
        return {'run_id':'one-run','status':'success','trace':[]}
    monkeypatch.setattr(application,'run_portfolio_agent',fake)
    first=asyncio.create_task(application.analyze_for_client('test','CLI-001',{'event_id':'same-agent'}))
    try:
        await started.wait()
        with pytest.raises(ValueError,match='still running'):
            await application.analyze_for_client('test','CLI-001',{'event_id':'same-agent'})
    finally:
        release.set()
        await first
    assert len(calls)==1


@pytest.mark.parametrize('message',['这个客户CLI-002比较保守','这个ACC-002A账户要保守一些'])
def test_chinese_adjacent_customer_or_account_id_is_checked(api,message):
    assert api.post('/api/portfolio/learning/CLI-001/notes',json={'message':message}).status_code==409
    assert not learning.Journal().rows('CLI-001')


def test_market_defaults_are_not_claimed_as_active_user_choices(api,monkeypatch):
    monkeypatch.setattr(application,'execute_task',lambda *_:{'run_id':'fake-quote','status':'success','result':{}})
    api.post('/api/portfolio/execute',json={'client_id':'CLI-001','task':'quote','symbol':'AAPL',
        'observation':{'event_id':'quote','parameter_sources':{'symbol':'default'}}})
    event=json.loads(learning.Journal().rows('CLI-001')[-1]['payload'])
    assert event['requested_parameters']=={}
    assert event['effective_parameters']['symbol']=='AAPL'
    assert event['parameter_sources']['symbol']=='default'
