"""Desktop package defaults stay ephemeral and user removals remain effective."""
import json
from pathlib import Path
import yaml
import pytest

@pytest.fixture
def desktop_defaults(tmp_path, monkeypatch):
    from hermes_cli import config
    home = tmp_path / 'home'
    home.mkdir()
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    defaults = runtime / 'default-mcp.json'
    defaults.write_text(json.dumps({
        'web': {'backend': 'local-open-websearch'},
        'plugins': {'bundled_dirs': ['${LLANO_LOCAL_MCP_ROOT}/default-plugins']},
        'mcp_servers': {'llano_websearch': {'command': '${LLANO_LOCAL_MCP_NODE}',
            'args': ['${LLANO_LOCAL_MCP_ROOT}/entry.js'], 'enabled': True}}
    }))
    monkeypatch.setenv('LLANO_LOCAL_MCP_CONFIG', str(defaults))
    monkeypatch.setenv('LLANO_LOCAL_MCP_ROOT', str(runtime))
    monkeypatch.setenv('LLANO_LOCAL_MCP_NODE', '/app/electron')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(config, 'get_hermes_home', lambda: home)
    monkeypatch.setattr(config, 'get_config_path', lambda: home / 'config.yaml')
    monkeypatch.setattr(config, 'get_env_path', lambda: home / '.env')
    return home, defaults, config


def test_defaults_are_readonly_and_saves_do_not_pin_package_paths(desktop_defaults):
    home, _, config = desktop_defaults
    loaded = config.load_config()
    assert loaded['web']['backend'] == 'local-open-websearch'
    assert loaded['mcp_servers']['llano_websearch']['command'] == '/app/electron'
    assert not (home / 'config.yaml').exists()
    loaded['display']['skin'] = 'test-user-skin'
    config.save_config(loaded)
    raw = yaml.safe_load((home / 'config.yaml').read_text())
    assert raw['display']['skin'] == 'test-user-skin'
    assert 'mcp_servers' not in raw
    assert not raw.get('plugins', {}).get('bundled_dirs')


def test_user_server_replaces_complete_default_and_backend_wins(desktop_defaults):
    home, _, config = desktop_defaults
    (home / 'config.yaml').write_text(yaml.safe_dump({'web': {'backend': 'custom'},
        'mcp_servers': {'llano_websearch': {'command': 'my-search', 'enabled': False}}}))
    loaded = config.load_config()
    assert loaded['web']['backend'] == 'custom'
    assert loaded['mcp_servers']['llano_websearch'] == {'command': 'my-search', 'enabled': False}


@pytest.mark.parametrize('action', ['remove', 'clear'])
def test_ui_removal_survives_reload(desktop_defaults, action):
    home, _, config = desktop_defaults
    from hermes_cli.mcp_config import _remove_mcp_server, _replace_mcp_servers
    if action == 'remove':
        assert _remove_mcp_server('llano_websearch')
    else:
        assert _replace_mcp_servers({}) == (True, [])
    assert config.load_config()['mcp_servers']['llano_websearch'] == {'enabled': False}
    raw = yaml.safe_load((home / 'config.yaml').read_text())
    assert raw['mcp_servers']['llano_websearch'] == {'enabled': False}


def test_runtime_defaults_invalidate_config_cache(desktop_defaults):
    _, defaults, config = desktop_defaults
    assert config.load_config()['web']['backend'] == 'local-open-websearch'
    defaults.write_text(json.dumps({'web': {'backend': 'changed-runtime-backend'}}))
    assert config.load_config()['web']['backend'] == 'changed-runtime-backend'


@pytest.mark.parametrize('override', [None, {'enabled': False},
    {'command': 'custom-search', 'args': ['custom.js'], 'enabled': True}])
def test_existing_profile_follows_updated_package_entry_without_overwriting_user_choices(desktop_defaults, override):
    home, defaults, config = desktop_defaults
    package_defaults = json.loads((Path(__file__).resolve().parents[2] /
        'apps/desktop/resources/local-mcp-runtime/default-mcp.json').read_text())
    old_defaults = json.loads(json.dumps(package_defaults))
    old_defaults['mcp_servers']['llano_websearch']['args'] = [
        '${LLANO_LOCAL_MCP_ROOT}/node_modules/open-websearch/build/index.js']
    defaults.write_text(json.dumps(old_defaults))
    user = {'display': {'skin': 'existing-profile'},
        'mcp_servers': {'personal': {'command': 'personal-server', 'args': []}}}
    if override is not None:
        user['mcp_servers']['llano_websearch'] = override
    config_path = home / 'config.yaml'
    config_path.write_text(yaml.safe_dump(user))
    unchanged = config_path.read_bytes()
    before = config.load_config()['mcp_servers']['llano_websearch']
    defaults.write_text(json.dumps(package_defaults))
    after = config.load_config()
    if override is None:
        assert before['args'][0].endswith('/build/index.js')
        assert after['mcp_servers']['llano_websearch']['args'] == [
            str(defaults.parent / 'node_modules/open-websearch/build/llano-stdio.mjs')]
    else:
        assert before == override
        assert after['mcp_servers']['llano_websearch'] == override
    assert after['mcp_servers']['personal'] == user['mcp_servers']['personal']
    assert after['display']['skin'] == 'existing-profile'
    assert config_path.read_bytes() == unchanged


def test_external_bundled_plugin_loads_and_explicit_disable_wins(desktop_defaults, monkeypatch):
    home, defaults, config = desktop_defaults
    from hermes_cli import plugins
    bundle = defaults.parent / 'default-plugins' / 'local-test'
    bundle.mkdir(parents=True)
    (bundle / 'plugin.yaml').write_text(yaml.safe_dump({'name': 'local-test', 'version': '1.0.0', 'kind': 'backend'}))
    (bundle / '__init__.py').write_text('def register(ctx):\n    pass\n')
    monkeypatch.setattr(plugins, 'get_bundled_plugins_dir', lambda: home / 'absent-bundled')
    manager = plugins.PluginManager()
    monkeypatch.setattr(manager, '_scan_entry_points', lambda: [])
    manager.discover_and_load()
    assert manager._plugins['local-test'].enabled
    assert manager._plugins['local-test'].manifest.source == 'bundled'
    (home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'disabled': ['local-test']}}))
    manager.discover_and_load(force=True)
    assert not manager._plugins['local-test'].enabled
    assert manager._plugins['local-test'].error == 'disabled via config'
    (home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'bundled_dirs': []}}))
    manager.discover_and_load(force=True)
    assert 'local-test' not in manager._plugins
