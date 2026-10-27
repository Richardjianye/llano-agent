"""Contract tests for the direct POSIX stdio MCP child watchdog."""

import os
import sys

import pytest

from tools import mcp_stdio_watchdog, mcp_tool


def test_is_orphaned_is_false_while_direct_parent_is_unchanged():
    original_ppid = 1234

    assert mcp_stdio_watchdog._is_orphaned(
        original_ppid,
        getppid=lambda: original_ppid,
    ) is False


@pytest.mark.skipif(os.name != "posix", reason="watchdog wrapping is POSIX-only")
def test_wrap_command_uses_stable_parent_pid_and_preserves_command_tail():
    parent_pid = os.getpid()
    command = "/opt/hermes/bin/mcp-server"
    command_args = ["--label", "value with spaces", "--", "literal-tail"]

    wrapped_command, wrapped_args = mcp_tool._wrap_command_with_watchdog(
        command,
        command_args,
    )

    assert wrapped_command == sys.executable
    assert wrapped_args == [
        os.path.join(os.path.dirname(mcp_tool.__file__), "mcp_stdio_watchdog.py"),
        "--ppid",
        str(parent_pid),
        "--",
        command,
        *command_args,
    ]
    assert "--create-time" not in wrapped_args


@pytest.mark.skipif(os.name != 'posix', reason='watchdog wrapping is POSIX-only')
def test_frozen_watchdog_dispatch_preserves_child_argv(monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    command = '/Applications/工作台.app/Contents/MacOS/工作台'
    args = ['/resources/mcp.js', 'argument with spaces']
    wrapped, tail = mcp_tool._wrap_command_with_watchdog(command, args)
    assert wrapped == sys.executable
    assert tail == ['--llano-mcp-watchdog', '--ppid', str(os.getpid()), '--', command, *args]


@pytest.mark.skipif(os.name != 'posix', reason='watchdog is POSIX-only')
def test_sidecar_dispatch_runs_watchdog_and_preserves_stdio():
    import subprocess
    from pathlib import Path
    entry = Path(__file__).resolve().parents[2] / 'apps/desktop/build-sidecar/sidecar_entry.py'
    result = subprocess.run([sys.executable, str(entry), '--llano-mcp-watchdog',
        '--ppid', str(os.getpid()), '--', sys.executable, '-c',
        'import sys; print(sys.stdin.readline().strip()+"-child")'],
        input='stdio', text=True, capture_output=True, timeout=15,
        env={**os.environ, 'PYTHONPATH': str(entry.parents[3])})
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'stdio-child\n'


@pytest.mark.skipif(os.name != 'posix', reason='watchdog signal forwarding is POSIX-only')
def test_sidecar_watchdog_shutdown_reaps_real_child(tmp_path):
    import subprocess
    import time
    from pathlib import Path
    entry = Path(__file__).resolve().parents[2] / 'apps/desktop/build-sidecar/sidecar_entry.py'
    pidfile = tmp_path / 'child.pid'
    proc = subprocess.Popen([sys.executable, str(entry), '--llano-mcp-watchdog',
        '--ppid', str(os.getpid()), '--', sys.executable, '-c',
        'import os,sys,time; open(sys.argv[1],"w").write(str(os.getpid())); time.sleep(120)',
        str(pidfile)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, 'PYTHONPATH': str(entry.parents[3])})
    child_pid = None
    try:
        deadline = time.monotonic() + 10
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert pidfile.exists()
        child_pid = int(pidfile.read_text())
        proc.terminate()
        assert proc.wait(timeout=10) == 143
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if child_pid:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass
