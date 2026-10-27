"""Windows installer regression coverage for Telegram's pinned webhook stack.

The installer runs only on Windows, so these source-level tests lock the
security contract on every CI host: a configured Telegram gateway is healthy
only when both PTB and Tornado are at the exact versions from ``pyproject``.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys


INSTALL_PS1 = Path(__file__).resolve().parents[1] / "scripts" / "install.ps1"


def _install_platform_sdks() -> str:
    source = INSTALL_PS1.read_text(encoding="utf-8")
    start = source.index("function Install-PlatformSdks")
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[brace : index + 1]
    raise AssertionError("unterminated Install-PlatformSdks function")


def _telegram_entry(body: str) -> str:
    start = body.index('Var = "TELEGRAM_BOT_TOKEN"')
    end = body.index('Var = "DISCORD_BOT_TOKEN"', start)
    return body[start:end]


def _telegram_health_check(entry: str) -> str:
    match = re.search(r'HealthCheck = "([^"]+)"', entry)
    assert match is not None
    return match.group(1)


def test_telegram_map_pins_ptb_and_tornado_exactly() -> None:
    entry = _telegram_entry(_install_platform_sdks())

    assert 'Spec = "python-telegram-bot[webhooks]==22.6"' in entry
    assert 'ExtraSpecs = @("tornado==6.5.8")' in entry
    assert "version('python-telegram-bot')" in entry
    assert "version('tornado')" in entry
    assert "('22.6', '6.5.8')" in entry
    assert "import telegram, tornado" in entry
    assert "assert " not in _telegram_health_check(entry)
    assert "raise SystemExit" in _telegram_health_check(entry)


def test_telegram_health_check_cannot_be_disabled_by_python_optimize() -> None:
    """A stale Tornado remains a hard failure under ``python -O``."""
    health_check = _telegram_health_check(
        _telegram_entry(_install_platform_sdks())
    )
    prelude = (
        "import importlib.metadata as metadata\n"
        "real_version = metadata.version\n"
        "metadata.version = lambda name: "
        "'6.5.7' if name == 'tornado' else real_version(name)\n"
    )
    env = os.environ.copy()
    env["PYTHONOPTIMIZE"] = "1"

    result = subprocess.run(
        [sys.executable, "-O", "-c", prelude + health_check],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 1, result.stderr


def test_telegram_health_check_detects_stale_installed_versions() -> None:
    body = _install_platform_sdks()
    probe = body.index('& $pythonExe -c $healthCheck')
    missing = body.index('$missing += $sdk', probe)

    assert '$sdk.ContainsKey("HealthCheck")' in body[:probe]
    assert probe < missing


def test_telegram_install_and_recovery_commands_include_tornado() -> None:
    body = _install_platform_sdks()

    assert '$installSpecs += @($sdk.ExtraSpecs)' in body
    assert '$pipArgs = @("-m", "pip", "install") + $installSpecs' in body
    assert '& $pythonExe @pipArgs' in body
    assert '$firstSpecs += @($missing[0].ExtraSpecs)' in body


def test_success_is_reported_only_after_post_install_health_check() -> None:
    body = _install_platform_sdks()
    install = body.index('& $pythonExe @pipArgs')
    post_install_probe = body.index('& $pythonExe -c $healthCheck', install)
    success = body.index('Write-Success "  Installed $($sdk.Import)"', install)

    assert install < post_install_probe < success
