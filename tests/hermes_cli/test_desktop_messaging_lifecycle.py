"""Desktop messaging lifecycle with real profile files and isolated children."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import json
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('executable',[
    '/Applications/工作台.app/Contents/Resources/sidecar/llano-sidecar',
    '"C:\\Program Files\\LLANO\\sidecar\\llano-sidecar.exe"',
])
def test_frozen_gateway_liveness_requires_runtime_subcommand(executable):
    from gateway.status import looks_like_gateway_runtime_command_line
    assert looks_like_gateway_runtime_command_line(executable+' gateway run --replace')
    assert looks_like_gateway_runtime_command_line(executable+' gateway restart')
    assert not looks_like_gateway_runtime_command_line(executable+' gateway status')
    assert not looks_like_gateway_runtime_command_line(executable+' serve --port 7912')
    assert not looks_like_gateway_runtime_command_line(executable+' --llano-probe')


@pytest.mark.parametrize('manager',['launchd','systemd (user)'])
def test_supervised_gateway_guard_checks_actual_service_home(monkeypatch,tmp_path,manager):
    import plistlib
    from hermes_cli import gateway as gc
    target=tmp_path/'desktop';other=tmp_path/'other-install'
    monkeypatch.setenv('HERMES_HOME',str(target))
    monkeypatch.setattr(gc,'_running_under_gateway_supervisor',lambda:False)
    monkeypatch.setattr(gc,'get_gateway_runtime_snapshot',lambda:SimpleNamespace(
        manager=manager,service_installed=True,service_running=True,service_scope='user'))
    plist=tmp_path/'gateway.plist'
    monkeypatch.setattr(gc,'get_launchd_plist_path',lambda:plist)
    service_home=[str(other)]
    monkeypatch.setattr(gc,'_hermes_home_from_systemd_unit_file',lambda **kw:service_home[0])
    plist.write_bytes(plistlib.dumps({'EnvironmentVariables':{'HERMES_HOME':str(other)}}))
    gc._guard_supervised_gateway_conflict()
    service_home[0]=str(target)
    plist.write_bytes(plistlib.dumps({'EnvironmentVariables':{'HERMES_HOME':str(target)}}))
    with pytest.raises(SystemExit):
        gc._guard_supervised_gateway_conflict()
    # Missing ownership data must keep the existing same-label protection.
    service_home[0]=None
    plist.write_bytes(plistlib.dumps({}))
    with pytest.raises(SystemExit):
        gc._guard_supervised_gateway_conflict()


def test_frozen_cli_does_not_pass_python_module_flag(monkeypatch):
    from hermes_cli.desktop_messaging import cli_command
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    assert cli_command(['gateway','run']) == [sys.executable,'gateway','run']
    monkeypatch.setattr(sys,'frozen',False)
    assert cli_command(['gateway','run']) == [sys.executable,'-m','hermes_cli.main','gateway','run']


def test_save_restart_child_uses_current_profile_credentials_and_stops(tmp_path,monkeypatch):
    from hermes_cli import desktop_messaging as dm
    home=tmp_path/'profile';home.mkdir()
    (home/'config.yaml').write_text('{}\n')
    (home/'.env').write_text('DINGTALK_CLIENT_ID=current-id\nDINGTALK_CLIENT_SECRET=current-secret\n')
    monkeypatch.setenv('DINGTALK_CLIENT_ID','old-parent-id')
    monkeypatch.setenv('DINGTALK_CLIENT_SECRET','old-parent-secret')
    output=home/'observed.json'
    code="import os,json,time,pathlib;pathlib.Path(os.environ['HERMES_HOME'],'observed.json').write_text(json.dumps({'home':os.environ['HERMES_HOME'],'id_current':os.environ.get('DINGTALK_CLIENT_ID')=='current-id','secret_current':os.environ.get('DINGTALK_CLIENT_SECRET')=='current-secret'}));time.sleep(30)"
    monkeypatch.setattr(dm,'cli_command',lambda args:[sys.executable,'-c',code])
    response=dm.control(home,'restart',messaging_keys={'DINGTALK_CLIENT_ID','DINGTALK_CLIENT_SECRET'})
    import time
    for _ in range(100):
        if output.exists():break
        time.sleep(.01)
    observed=json.loads(output.read_text())
    assert observed=={'home':str(home.resolve()),'id_current':True,'secret_current':True}
    assert 'current-secret' not in json.dumps(response)
    proc=dm._children[str(home.resolve())]
    assert proc.poll() is None
    output.unlink()
    (home/'.env').write_text('DINGTALK_CLIENT_ID=updated-id\nDINGTALK_CLIENT_SECRET=updated-secret\n')
    updated_code=code.replace('current-id','updated-id').replace('current-secret','updated-secret')
    monkeypatch.setattr(dm,'cli_command',lambda args:[sys.executable,'-c',updated_code])
    dm.control(home,'restart',messaging_keys={'DINGTALK_CLIENT_ID','DINGTALK_CLIENT_SECRET'})
    for _ in range(100):
        if output.exists():break
        time.sleep(.01)
    assert json.loads(output.read_text())['secret_current'] is True
    assert proc.poll() is not None
    proc=dm._children[str(home.resolve())]
    dm.control(home,'stop',messaging_keys=set())
    assert proc.poll() is not None
    assert os.environ['DINGTALK_CLIENT_SECRET']=='old-parent-secret'


def test_dingtalk_explicit_disable_survives_keys(tmp_path,monkeypatch,_isolate_hermes_home):
    from hermes_constants import get_hermes_home
    from gateway.config import load_gateway_config,Platform
    home=get_hermes_home();home.mkdir(exist_ok=True)
    (home/'config.yaml').write_text('platforms:\n  dingtalk:\n    enabled: false\n')
    monkeypatch.setenv('DINGTALK_CLIENT_ID','configured-id')
    monkeypatch.setenv('DINGTALK_CLIENT_SECRET','configured-secret')
    platform=load_gateway_config().platforms[Platform.DINGTALK]
    assert platform.enabled is False
    assert bool(platform.extra.get('client_secret'))


def test_configured_stopped_does_not_require_setup(monkeypatch,tmp_path,_isolate_hermes_home):
    from hermes_cli import web_server as ws
    from hermes_constants import get_hermes_home
    home=get_hermes_home();home.mkdir(exist_ok=True)
    (home/'config.yaml').write_text('platforms:\n  dingtalk:\n    enabled: true\n')
    (home/'.env').write_text('DINGTALK_CLIENT_ID=from-file-id\nDINGTALK_CLIENT_SECRET=from-file-secret\n')
    monkeypatch.delenv('DINGTALK_CLIENT_ID',raising=False)
    monkeypatch.delenv('DINGTALK_CLIENT_SECRET',raising=False)
    monkeypatch.setattr(ws,'resolve_gateway_liveness',lambda **kw:SimpleNamespace(running=False))
    # Optional adapter registry may be absent in a broken installation.
    from gateway.config import GatewayConfig
    monkeypatch.setattr(GatewayConfig,'_is_platform_connected',lambda *args:False)
    entry=ws._build_catalog_entry('dingtalk')
    row=ws._messaging_platform_payload(entry,ws.load_env(),None)
    assert row['enabled'] and row['configured']
    assert row['state']=='gateway_stopped'
    assert all(field['is_set'] for field in row['env_vars'] if field['required'])
    assert 'from-file-secret' not in json.dumps(row)


def test_external_secret_source_is_reported_configured(monkeypatch,_isolate_hermes_home):
    from hermes_cli import web_server as ws
    from hermes_constants import get_hermes_home
    home=get_hermes_home();home.mkdir(exist_ok=True)
    (home/'config.yaml').write_text('platforms:\n  dingtalk:\n    enabled: true\n')
    monkeypatch.setattr('hermes_cli.env_loader.hydrate_profile_secret_sources',lambda path:{'DINGTALK_CLIENT_ID':'external-id','DINGTALK_CLIENT_SECRET':'external-secret'})
    monkeypatch.setattr(ws,'resolve_gateway_liveness',lambda **kw:SimpleNamespace(running=False))
    row=ws._messaging_platform_payload(ws._build_catalog_entry('dingtalk'),{},None,scoped=True,profile_home=home)
    assert row['configured'] and row['state']=='gateway_stopped'
    assert 'external-secret' not in json.dumps(row)


def test_messaging_password_json_exposes_presence_only(monkeypatch,_isolate_hermes_home):
    from hermes_cli import web_server as ws
    from hermes_constants import get_hermes_home
    home=get_hermes_home();home.mkdir(exist_ok=True)
    (home/'config.yaml').write_text('platforms:\n  dingtalk:\n    enabled: true\n')
    secret='unique-secret-prefix-abcdefghijklmnopqrstuvwxyz-unique-secret-suffix'
    monkeypatch.setattr(ws,'resolve_gateway_liveness',lambda **kw:SimpleNamespace(running=False))
    row=ws._messaging_platform_payload(ws._build_catalog_entry('dingtalk'),{
        'DINGTALK_CLIENT_ID':'test-client-id',
        'DINGTALK_CLIENT_SECRET':secret,
    },None,scoped=True,profile_home=home)
    field=next(item for item in row['env_vars'] if item['key']=='DINGTALK_CLIENT_SECRET')
    assert field['is_password'] and field['is_set']
    assert field['redacted_value'] is None
    payload=json.dumps(row)
    assert secret not in payload
    assert secret[:6] not in payload
    assert secret[-4:] not in payload


def test_desktop_messaging_restart_stopped_uses_own_child(monkeypatch,_isolate_hermes_home):
    from hermes_cli import web_server as ws
    monkeypatch.setenv('HERMES_DESKTOP','1')
    monkeypatch.setattr(ws,'_messaging_platform_payload',lambda *args,**kw:{'enabled':True,'configured':True})
    observed=[]
    monkeypatch.setattr('hermes_cli.desktop_messaging.control',lambda home,action,**kw:observed.append(action) or {'ok':True,'state':'starting','gateway_running':False})
    assert asyncio.run(ws.control_messaging_gateway('restart'))['state']=='starting'
    assert observed==['restart']


def test_app_reopen_restores_only_enabled_configured_channels(monkeypatch,_isolate_hermes_home):
    from hermes_cli import web_server as ws
    from hermes_constants import get_hermes_home
    home=get_hermes_home();home.mkdir(exist_ok=True)
    (home/'config.yaml').write_text('platforms:\n  dingtalk:\n    enabled: true\n')
    (home/'.env').write_text('DINGTALK_CLIENT_ID=stored-id\nDINGTALK_CLIENT_SECRET=stored-secret\n')
    monkeypatch.setattr('gateway.status.get_running_pid',lambda **kw:None)
    monkeypatch.setattr(ws,'resolve_gateway_liveness',lambda **kw:SimpleNamespace(running=False))
    calls=[]
    monkeypatch.setattr('hermes_cli.desktop_messaging.control',lambda home,action,**kw:calls.append((home,action)))
    ws._restore_desktop_messaging_gateway()
    assert calls==[(home,'start')]
    calls.clear()
    (home/'config.yaml').write_text('platforms:\n  dingtalk:\n    enabled: false\n')
    ws._restore_desktop_messaging_gateway()
    assert not calls


def test_desktop_empty_profile_does_not_inherit_parent_credentials(monkeypatch,_isolate_hermes_home):
    from hermes_cli import web_server as ws
    from hermes_constants import get_hermes_home
    home=get_hermes_home();home.mkdir(exist_ok=True)
    (home/'config.yaml').write_text('platforms:\n  dingtalk:\n    enabled: true\n')
    monkeypatch.setenv('HERMES_DESKTOP','1')
    monkeypatch.setenv('DINGTALK_CLIENT_ID','other-profile-id')
    monkeypatch.setenv('DINGTALK_CLIENT_SECRET','other-profile-secret')
    monkeypatch.setattr(ws,'resolve_gateway_liveness',lambda **kw:SimpleNamespace(running=False))
    row=ws._messaging_platform_payload(ws._build_catalog_entry('dingtalk'),{},None)
    assert row['configured'] is False
    assert row['state']=='not_configured'
    assert not any(field['is_set'] for field in row['env_vars'] if field['required'])
