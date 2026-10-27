from contextlib import contextmanager
from types import SimpleNamespace
import json
import pytest
import tui_gateway.server as server
from agent.conversation_mode import CHAT_TOOLS, normalize_mode, row_mode, tool_allowed
from hermes_state import SessionDB

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.delenv('HERMES_MANAGED_DIR', raising=False)
    db = SessionDB(db_path=tmp_path / 'state.db')
    @contextmanager
    def profile_db(params): yield db
    monkeypatch.setattr(server, '_profile_db', profile_db)
    monkeypatch.setattr(server, '_get_db', lambda: db)
    monkeypatch.setattr(server, '_schedule_agent_build', lambda *_: None)
    monkeypatch.setattr(server, '_schedule_session_cap_enforcement', lambda: None)
    monkeypatch.setattr(server, '_resolve_model', lambda: 'fixture-model')
    monkeypatch.setattr(server, '_profile_home', lambda *_: None)
    monkeypatch.setattr(server, '_sessions', {})
    yield db
    db.close()


def create(mode='chat', **kwargs):
    return server._methods['session.create']('r', {'mode': mode, 'source':'desktop', **kwargs})


def test_mode_persists_in_real_sqlite_without_creating_abandoned_drafts(runtime):
    response = create()
    result = response['result']
    sid = result['session_id']; key = result['stored_session_id']
    assert result['info']['mode'] == 'chat'
    assert runtime.get_session(key) is None
    server._ensure_session_db_row(server._sessions[sid])
    assert row_mode(runtime.get_session(key)) == 'chat'
    assert server._stored_session_runtime_overrides(runtime.get_session(key))['conversation_mode'] == 'chat'
    runtime.append_message(key, 'user', '你好')
    listed = server._methods['session.list']('l', {})['result']['sessions']
    assert next(s for s in listed if s['id']==key)['mode'] == 'chat'
    compact = runtime.list_sessions_rich(compact_rows=True)
    assert next(s for s in compact if s['id']==key)['mode'] == 'chat'


def test_related_work_is_empty_and_preserves_original_mode(runtime):
    runtime.create_session('original', source='desktop', model_config={'conversation_mode':'chat'})
    runtime.append_message('original','user','原会话')
    result = create('work', related_session_id='original')['result']
    assert result['info']['mode']=='work'
    assert result['info']['related_session_id']=='original'
    assert result['messages']==[]
    server._ensure_session_db_row(server._sessions[result['session_id']])
    row = runtime.get_session(result['stored_session_id'])
    assert row_mode(row)=='work' and json.loads(row['model_config'])['related_session_id']=='original'
    assert row_mode(runtime.get_session('original'))=='chat'


def test_invalid_mode_and_foreign_related_id_create_no_runtime(runtime):
    for mode in ['unknown', True, 1, None]:
        assert 'error' in create(mode)
    assert 'error' in create('chat', related_session_id='another-profile-row')
    assert not server._sessions


def test_legacy_is_work_and_branch_inherits_without_mode_mutation(runtime):
    runtime.create_session('legacy', source='desktop')
    assert row_mode(runtime.get_session('legacy'))=='work'
    runtime.create_session('chat-parent', source='desktop',model_config={'conversation_mode':'chat'})
    result=server._methods['session.create']('b',{'source':'desktop','parent_session_id':'chat-parent'})
    assert result['result']['info']['mode']=='chat'
    assert 'error' in create('work',parent_session_id='chat-parent')


def test_runtime_settings_preserve_mode_but_explicit_changes_fail(runtime):
    runtime.create_session('chat',source='desktop',model_config={'conversation_mode':'chat'})
    runtime.update_session_meta('chat',json.dumps({'model':'new'}))
    assert row_mode(runtime.get_session('chat'))=='chat'
    for new in ['work',None]:
        with pytest.raises(ValueError):runtime.patch_session_model_config('chat',{'conversation_mode':new})
    runtime.patch_session_model_config('chat',{'service_tier':'priority'})
    assert row_mode(runtime.get_session('chat'))=='chat'


@pytest.mark.parametrize('name',['terminal','write_file','patch','browser_click','delegate_task','tool_search','tool_call','mcp_external','skill_view'])
def test_chat_denies_before_plugins_or_dispatch(name):
    from agent.tool_executor import _run_agent_tool_execution_middleware
    from agent.agent_runtime_helpers import invoke_tool
    agent=SimpleNamespace(conversation_mode='chat')
    def side_effect(_):pytest.fail('forbidden execution')
    result=_run_agent_tool_execution_middleware(agent,function_name=name,function_args={},effective_task_id='test',tool_call_id='id',execute=side_effect)
    assert result.blocked and not result.dispatched
    assert json.loads(result.result)['reason']=='conversation_mode_restricted'
    assert json.loads(invoke_tool(agent,name,{},'test',skip_tool_execution_middleware=True))['mode']=='chat'


def test_work_retains_existing_tools_chat_never_gains_mcp_after_refresh():
    from tools.mcp_tool import refresh_agent_mcp_tools
    work=SimpleNamespace(conversation_mode='work')
    chat=SimpleNamespace(conversation_mode='chat',tools=[{'function':{'name':'web_search'}}])
    for name in ['terminal','delegate_task','mcp_external']:assert tool_allowed(work,name)
    assert {n for n in ['web_search','web_extract','terminal'] if tool_allowed(chat,n)}==CHAT_TOOLS
    original=chat.tools
    assert refresh_agent_mcp_tools(chat)==set() and chat.tools is original


def test_slash_and_cli_exec_cannot_start_work_in_chat(runtime):
    result=create()['result'];sid=result['session_id']
    for method,params in [('slash.exec',{'command':'/terminal echo nope'}),('command.dispatch',{'name':'tools','arg':'enable terminal'}),('cli.exec',{'argv':['chat']})]:
        response=server._methods[method]('x',{'session_id':sid,**params})
        assert response['error']['code']==4031
    catalog=server._methods['commands.catalog']('c',{'session_id':sid})['result']
    assert not catalog['skills'] and all(p[0].lstrip('/') not in {'tools','skills','terminal'} for p in catalog['pairs'])



def test_metadata_without_runtime_record_still_reports_agent_mode(runtime):
    agent = SimpleNamespace(conversation_mode='chat', _related_session_id='origin', model='fixture')
    info = server._session_info(agent)
    assert info['mode'] == 'chat' and info['related_session_id'] == 'origin'
    lazy = server._fallback_session_info({'mode':'chat','session_key':'fixture','cwd':str(runtime.db_path.parent)})
    assert lazy['mode']=='chat'


def test_compute_process_frame_carries_frozen_mode_and_relation(runtime):
    import threading
    record = {'mode':'chat','related_session_id':'origin','session_key':'chat','history_lock':threading.Lock(),'history':[],'cwd':str(runtime.db_path.parent)}
    frame = server._compute_host_turn_frame('rid','sid',record,'hello')
    assert frame['mode']=='chat' and frame['related_session_id']=='origin'


def test_corrupt_legacy_metadata_does_not_break_lists():
    assert row_mode({'model_config':'bad old metadata'})=='work'


def test_chat_tool_budget_blocks_before_dispatch():
    import threading
    from agent.tool_executor import _run_agent_tool_execution_middleware
    agent=SimpleNamespace(conversation_mode='chat',_conversation_mode_budget_lock=threading.Lock(),_conversation_mode_tool_calls=4)
    result=_run_agent_tool_execution_middleware(agent,function_name='web_search',function_args={},effective_task_id='test',tool_call_id='id',execute=lambda *_: pytest.fail('budget must reject'))
    assert result.blocked and not result.dispatched and json.loads(result.result)['reason']=='chat_tool_budget'



def test_chat_builtin_name_cannot_be_redefined_to_execute(runtime, monkeypatch):
    result=create()['result']
    monkeypatch.setattr(server, '_load_cfg', lambda: {'quick_commands': {'help': {'type':'exec','command':'unsafe'}}})
    monkeypatch.setattr(server.subprocess, 'run', lambda *_a, **_kw: pytest.fail('quick command must not execute'))
    for method, params in [('command.dispatch',{'name':'help'}),('slash.exec',{'command':'/help'})]:
        response=server._methods[method]('h',{'session_id':result['session_id'],**params})
        assert 'result' in response


@pytest.mark.parametrize('transport', [{'api_mode':'codex_app_server'}, {'provider':'copilot-acp'}, {'base_url':'acp+tcp://localhost'}])
def test_chat_rejects_agent_transports_that_can_execute_outside_tool_scope(transport):
    from agent.conversation_mode import validate_chat_transport
    with pytest.raises(ValueError):validate_chat_transport('chat',**transport)
    validate_chat_transport('work',**transport)



def test_chat_does_not_enable_search_disabled_in_profile():
    from agent.conversation_mode import configured_chat_tools
    assert configured_chat_tools([])==frozenset()
    assert configured_chat_tools(['terminal'])==frozenset()
    assert configured_chat_tools(['web'])==CHAT_TOOLS
    assert not tool_allowed(SimpleNamespace(conversation_mode='chat',_conversation_mode_allowed_tools=frozenset()),'web_search')


def test_chat_mcp_reload_rejected_before_global_discovery(runtime, monkeypatch):
    sid = create()['result']['session_id']
    import tools.mcp_tool as mcp
    monkeypatch.setattr(mcp, 'shutdown_mcp_servers', lambda: pytest.fail('must not stop work servers'))
    monkeypatch.setattr(mcp, 'discover_mcp_tools', lambda: pytest.fail('must not start MCP processes'))
    response = server._methods['reload.mcp']('reload', {'session_id': sid, 'confirm': True})
    assert response['error']['code'] == 4031


@pytest.mark.parametrize('mode', ['chat', 'work'])
def test_branded_cold_resume_preserves_mode_through_first_agent_build(
    runtime, tmp_path, monkeypatch, mode
):
    """Provider rebinding must not erase the session identity before init."""
    import yaml
    from hermes_cli.managed_scope import invalidate_managed_cache
    from run_agent import AIAgent

    managed = tmp_path / 'managed-resume'
    managed.mkdir()
    (managed / 'config.yaml').write_text(yaml.safe_dump({'product': {
        'enabled': True, 'brand_id': 'llano-ai-workbench',
        'provider_id': 'llano-gateway',
        'disabled_capabilities': ['provider_management', 'nous_services'],
    }}))
    monkeypatch.setenv('HERMES_MANAGED_DIR', str(managed))
    invalidate_managed_cache()
    key = f'branded-old-{mode}'
    runtime.create_session(key, source='desktop', model='fixture-model', model_config={
        'conversation_mode': mode, 'related_session_id': 'original-chat',
        'provider': 'personal-provider', 'base_url': 'https://personal.invalid/v1',
    })
    runtime.append_message(key, 'user', '旧问题')
    runtime.append_message(key, 'assistant', '旧回答')

    captured = {}
    def make_agent(_sid, _key, **kwargs):
        captured.update(kwargs)
        # Keep the real constructor's SQLite frozen-mode guard. Only its
        # unrelated provider/network initialization is stubbed for this test.
        return AIAgent(conversation_mode=kwargs.get('conversation_mode'),
                       session_id=kwargs.get('session_id'), session_db=runtime)
    def init_agent(agent, **kwargs):
        agent._session_init_model_config = {}
        agent.tools = []
        agent.valid_tool_names = set()
        agent._session_db = runtime
    monkeypatch.setattr('agent.agent_init.init_agent', init_agent)
    monkeypatch.setattr(server, '_make_agent', make_agent)
    monkeypatch.setattr('tui_gateway.entry.ensure_mcp_discovery_started', lambda: None)
    for name, replacement in {
        '_emit': lambda *a, **kw: None,
        '_enable_gateway_prompts': lambda: None,
        '_wire_callbacks': lambda *_: None,
        '_config_model_target': lambda: None,
        '_load_memory_notifications': lambda: False,
        '_start_notification_poller': lambda *a: None,
        '_notify_session_boundary': lambda *a: None,
        '_session_info': lambda *a: {},
        '_probe_config_health': lambda *_: None,
        '_load_cfg': lambda: {},
        '_schedule_mcp_late_refresh': lambda *a: None,
        '_maybe_schedule_auto_continue': lambda *a: None,
    }.items():
        monkeypatch.setattr(server, name, replacement)

    response = server._methods['session.resume']('resume', {'session_id': key, 'source': 'desktop'})
    assert 'error' not in response, response
    result = response['result']
    assert result['info']['mode'] == mode
    record = server._sessions[result['session_id']]
    assert record['mode'] == mode
    assert record['related_session_id'] == 'original-chat'
    server._start_agent_build(result['session_id'], record)
    assert record['agent_ready'].wait(timeout=10)
    assert record['agent_error'] is None
    assert record['agent'].conversation_mode == mode
    assert captured['conversation_mode'] == mode
    assert captured['related_session_id'] == 'original-chat'
    assert captured['provider_override'] == 'llano-gateway'
    assert captured['model_override']['provider'] == 'llano-gateway'
    assert captured['model_override']['base_url'] is None
    assert 'personal-provider' not in json.dumps(captured, default=str)
    assert row_mode(runtime.get_session(key)) == mode
    invalidate_managed_cache()
