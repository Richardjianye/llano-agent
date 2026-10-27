"""Desktop-owned messaging subprocesses, independent of the chat sidecar."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading

_lock = threading.RLock()
_children: dict[str, subprocess.Popen] = {}


def cli_command(arguments: list[str]) -> list[str]:
    """A frozen entry point is a CLI executable, not a Python interpreter."""
    return [sys.executable, *([] if getattr(sys, 'frozen', False) else ['-m', 'hermes_cli.main']), *arguments]


def _stop_child(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def control(home: Path, action: str, *, messaging_keys: set[str]) -> dict:
    from agent.secret_scope import build_profile_secret_scope
    from hermes_cli.env_loader import hydrate_profile_secret_sources
    from hermes_cli._subprocess_compat import windows_detach_popen_kwargs

    key = str(home.resolve())
    with _lock:
        previous = _children.get(key)
        if action == 'stop':
            if previous is not None:
                _stop_child(previous)
                _children.pop(key, None)
            return {'ok': True, 'pid': None, 'name': 'messaging-gateway', 'state': 'stopped', 'gateway_running': False}
        if action == 'start' and previous is not None and previous.poll() is None:
            return {'ok': True, 'pid': previous.pid, 'name': 'messaging-gateway', 'state': 'starting', 'gateway_running': True}
        if previous is not None:
            _stop_child(previous)
        hydrate_profile_secret_sources(home)
        values = build_profile_secret_scope(home)
        env = dict(os.environ)
        env.pop('_HERMES_GATEWAY', None)
        env['HERMES_HOME'] = key
        env['HERMES_NONINTERACTIVE'] = '1'
        # A profile's messaging secrets must not inherit another profile's
        # startup environment. The child reloads its own dotenv/sources too.
        for name in messaging_keys:
            env.pop(name, None)
            if values.get(name):
                env[name] = values[name]
        log_dir = home / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / 'messaging-gateway.log').open('ab', buffering=0) as log:
            proc = subprocess.Popen(cli_command(['gateway', 'run', '--replace']),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                cwd=key, env=env, **windows_detach_popen_kwargs())
        _children[key] = proc
        return {'ok': True, 'pid': proc.pid, 'name': 'messaging-gateway', 'state': 'starting', 'gateway_running': False}


def shutdown() -> None:
    with _lock:
        for proc in list(_children.values()):
            _stop_child(proc)
        _children.clear()
