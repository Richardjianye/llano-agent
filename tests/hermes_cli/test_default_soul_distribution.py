"""Generic installers stay stock; managed runtime selects branded persona."""

from __future__ import annotations

import re
from pathlib import Path

from hermes_cli.default_soul import (
    BRANDED_DEFAULT_SOUL_MD,
    STOCK_DEFAULT_SOUL_MD,
)
from hermes_cli.default_prompt_policy import BUNDLED_DEFAULT_PERSONA


REPO_ROOT = Path(__file__).resolve().parents[2]


def _normalized(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").strip()


def test_generic_installers_and_docker_seed_stock_default_soul():
    install_sh = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    shell_match = re.search(
        r"<< 'SOUL_EOF'\n(?P<body>.*?)\nSOUL_EOF", install_sh, re.DOTALL
    )
    assert shell_match is not None

    install_ps1 = (REPO_ROOT / "scripts" / "install.ps1").read_text(
        encoding="utf-8"
    )
    powershell_match = re.search(
        r'\$soulContent = @"\n(?P<body>.*?)\n"@', install_ps1, re.DOTALL
    )
    assert powershell_match is not None

    docker_soul = (REPO_ROOT / "docker" / "SOUL.md").read_text(encoding="utf-8")
    expected = _normalized(STOCK_DEFAULT_SOUL_MD)
    assert _normalized(shell_match.group("body")) == expected
    assert _normalized(powershell_match.group("body")) == expected
    assert _normalized(docker_soul) == expected


def test_bundled_managed_policy_uses_branded_default_persona():
    assert BUNDLED_DEFAULT_PERSONA == BRANDED_DEFAULT_SOUL_MD


def test_every_shipped_branded_default_stays_recognized_as_default():
    """每一版发过的品牌默认都必须仍被认成「已知默认」。

    换人格时把上一版原文挪出已知默认集合，会让 SOUL.md 恰好等于该原文的机器
    被误判成「用户自定义」：网关下发的人格不再替换它
    （agent/system_prompt.py 的 is_known_default_soul 分支），种子逻辑也不再
    升级它（hermes_cli/config.py 的 _ensure_default_soul_md）。两边都静默失效。
    """
    from hermes_cli.default_soul import (
        PREVIOUS_BRANDED_DEFAULT_SOUL,
        RETIRED_BRANDED_DEFAULT_SOUL_XIAOMA,
        is_known_default_soul,
    )

    shipped = (
        STOCK_DEFAULT_SOUL_MD,
        BRANDED_DEFAULT_SOUL_MD,
        PREVIOUS_BRANDED_DEFAULT_SOUL,
        RETIRED_BRANDED_DEFAULT_SOUL_XIAOMA,
    )
    for text in shipped:
        assert is_known_default_soul(text), f"不再被认成已知默认：{text[:20]}"

    # 用户真的写过东西的 SOUL.md 不能被当成默认值（否则会被覆盖）。
    assert not is_known_default_soul("我的自定义人格")
    assert not is_known_default_soul("  " + BRANDED_DEFAULT_SOUL_MD + "\n我自己补了一句")


def test_windows_installer_writes_soul_as_utf8_without_bom():
    install_ps1 = (REPO_ROOT / "scripts" / "install.ps1").read_text(
        encoding="utf-8"
    )

    assert "New-Object System.Text.UTF8Encoding($false)" in install_ps1
    assert (
        "[System.IO.File]::WriteAllText($soulPath, $soulContent, $utf8NoBom)"
        in install_ps1
    )
