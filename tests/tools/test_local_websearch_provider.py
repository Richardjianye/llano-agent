"""Local provider contract and private MCP transport boundaries."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def provider():
    path = Path(__file__).parents[2] / 'apps/desktop/resources/default-plugins/llano-local-websearch/__init__.py'
    spec = importlib.util.spec_from_file_location('test_local_search_plugin', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, module.LocalWebSearchProvider()


def test_search_single_engine_bounded_and_failures(provider, monkeypatch):
    module, instance = provider
    monkeypatch.setattr(instance, 'is_available', lambda: True)
    monkeypatch.setattr(module, '_allowed', lambda url: None)
    calls = []
    def call(tool, args):
        calls.append((tool, args))
        return {'results': [{'title': 'A', 'url': 'https://example.com', 'description': 'B'}]}
    monkeypatch.setattr(module, '_call', call)
    assert instance.search('q', 100)['data']['web'][0]['position'] == 1
    assert calls == [('search', {'query': 'q', 'limit': 5, 'engines': ['bing']})]
    monkeypatch.setattr(module, '_call', lambda *a: {'results': [], 'partialFailures': [{'message': 'blocked'}]})
    assert instance.search('q')['success'] is False


def test_extract_request_only_policy_and_empty(provider, monkeypatch):
    module, instance = provider
    monkeypatch.setattr(instance, 'is_available', lambda: True)
    monkeypatch.setattr(module, '_allowed', lambda url: 'policy blocked' if 'blocked' in url else None)
    calls = []
    def call(tool, args):
        calls.append((tool, args))
        return {'content': 'actual page', 'title': 'Page', 'finalUrl': args['url'], 'retrievalMethod': 'request'}
    monkeypatch.setattr(module, '_call', call)
    rows = instance.extract(['https://example.com', 'https://blocked.example'], renderMode='browser')
    assert rows[0]['content'] == 'actual page'
    assert rows[1]['error'] == 'policy blocked'
    assert len(calls) == 1 and calls[0][1]['renderMode'] == 'request'
    monkeypatch.setattr(module, '_call', lambda *a: {'content': ''})
    assert instance.extract(['https://example.com'])[0]['error']


@pytest.mark.parametrize('filters,allowed', [({'include':['search'],'exclude':['*']},True), ({'include':['fetchWebContent']},False), ({'exclude':['search']},False)])
def test_private_transport_one_server_and_filters(monkeypatch, filters, allowed):
    from tools import mcp_tool as m
    monkeypatch.setattr(m, '_load_mcp_config', lambda: {'local': {'command': 'node', 'tools': filters}, 'other': {'command':'other'}})
    monkeypatch.setattr(m, '_servers', {})
    monkeypatch.setattr(m, '_lazy_server_configs', {})
    registrations = []
    monkeypatch.setattr(m, 'register_mcp_servers', lambda cfg: registrations.append(cfg))
    monkeypatch.setattr(m, '_make_tool_handler', lambda *a: lambda args: 'actual')
    result = m.call_provider_mcp_tool('local', 'search', {'query':'q'})
    if allowed:
        assert result == 'actual'
        assert set(registrations[0]) == {'local'}
        assert registrations[0]['local']['_provider_only'] is True
        assert registrations[0]['local']['tools'] == filters
    else:
        assert 'disabled' in result and not registrations


def test_private_connection_upgrade_for_work(monkeypatch):
    from tools import mcp_tool as m
    server = SimpleNamespace(_config={'_provider_only':True,'command':'node'}, session=object())
    monkeypatch.setattr(m, '_servers', {'local':server})
    monkeypatch.setattr(m, '_server_connecting', set())
    monkeypatch.setattr(m, '_lazy_server_configs', {})
    monkeypatch.setattr(m, '_ensure_mcp_sdk', lambda: True)
    calls=[]
    monkeypatch.setattr(m, '_register_server_tools', lambda name,srv,cfg: calls.append((name,cfg)) or ['mcp_local_search'])
    config={'command':'node', 'tools':{'include':['search']}}
    m.register_mcp_servers({'local':config})
    assert calls == [('local',config)]
    assert server._config == config
    assert server._registered_tool_names == ['mcp_local_search']


def test_secret_source_inheritance_opt_out(monkeypatch):
    from tools import mcp_tool as m
    monkeypatch.setenv('COMPANY_GATEWAY_API_KEY','sensitive-value')
    monkeypatch.setattr('hermes_cli.env_loader.get_secret_source', lambda key: 'source' if key == 'COMPANY_GATEWAY_API_KEY' else None)
    assert 'COMPANY_GATEWAY_API_KEY' not in m._build_safe_env({}, inherit_secret_sources=False)
    assert m._build_safe_env({})['COMPANY_GATEWAY_API_KEY'] == 'sensitive-value'
    assert m._build_safe_env({'EXPLICIT':'configured'}, inherit_secret_sources=False)['EXPLICIT'] == 'configured'


def test_provider_transport_change_fails_without_call(monkeypatch):
    from tools import mcp_tool as m
    monkeypatch.setattr(m, '_load_mcp_config', lambda: {'local':{'command':'new-node'}})
    monkeypatch.setattr(m, '_servers', {'local':SimpleNamespace(_config={'command':'old-node'})})
    assert 'configuration changed' in m.call_provider_mcp_tool('local','search',{})


def test_concurrent_private_start_then_work_publication(monkeypatch):
    import threading
    from tools import mcp_tool as m
    event=threading.Event()
    servers={}
    monkeypatch.setattr(m,'_servers',servers)
    monkeypatch.setattr(m,'_provider_connection_events',{'local':event})
    monkeypatch.setattr(m,'_server_connecting',set())
    monkeypatch.setattr(m,'_lazy_server_configs',{})
    monkeypatch.setattr(m,'_ensure_mcp_sdk',lambda:True)
    config={'command':'node'}
    server=SimpleNamespace(_config={**config,'_provider_only':True},session=object())
    monkeypatch.setattr(m,'_register_server_tools',lambda *args:['mcp_local_search'])
    def finish():
        servers['local']=server
        event.set()
    timer=threading.Timer(0.05,finish)
    timer.start()
    m.register_mcp_servers({'local':config})
    timer.join()
    assert server._registered_tool_names==['mcp_local_search']


def test_parse_real_mcp_handler_envelope(provider,monkeypatch):
    module,instance=provider
    from tools import mcp_tool as m
    monkeypatch.setattr(m,'_load_mcp_config',lambda:{'llano_websearch':{'command':'node','env':{'MODE':'stdio','SEARCH_MODE':'request'}}})
    monkeypatch.setattr(m,'call_provider_mcp_tool',lambda *args:json.dumps({'result':json.dumps({'results':[]})}))
    assert module._call('search',{})=={'results':[]}
    monkeypatch.setattr(m,'call_provider_mcp_tool',lambda *args:json.dumps({'error':'transport failed'}))
    with pytest.raises(RuntimeError,match='transport failed'):
        module._call('search',{})


def test_browser_or_remote_override_cannot_escape_request_mode(provider,monkeypatch):
    module,_=provider
    from tools import mcp_tool as m
    monkeypatch.setattr(m,'_load_mcp_config',lambda:{'llano_websearch':{'command':'node','env':{'MODE':'stdio','SEARCH_MODE':'playwright'}}})
    with pytest.raises(RuntimeError,match='纯 HTTP'):
        module._call('search',{})
