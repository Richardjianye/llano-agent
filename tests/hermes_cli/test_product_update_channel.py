"""阶段 5 · U5：更新旁路封堵

自有更新闭环成立的前提是**别的更新通道都走不通**。只要还剩一条能从
NousResearch 官方仓库拉源码的路，签名清单那一整套就只是「多了一个可选通道」，
而不是「唯一通道」。

这里逐条覆盖已知的旁路。每条都成对断言：品牌构建下必须拒绝，stock Hermes 下
必须放行 —— 只测前者的话，一个「把所有人都拒了」的实现也能全绿，
而那会把上游功能一起弄坏。

能力 × 通道的完备性由 test_product_capability_matrix.py 保证（self_update 那一行）。
本文件补的是矩阵覆盖不到的两类：Electron IPC 那条通道，以及三条 Nous 官方
内容/遥测通道的具体行为。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hermes_cli import commands as hermes_commands
from hermes_cli import product_cli_gate, product_policy

REPO_ROOT = Path(__file__).resolve().parents[2]


def _branded(*disabled: str):
    return {
        "product": {
            "enabled": True,
            "brand_id": "llano-ai-workbench",
            "provider_id": "llano-gateway",
            "disabled_capabilities": list(disabled),
            "allowed_messaging_platforms": ["telegram"],
        }
    }


@pytest.fixture
def branded(monkeypatch):
    def _apply(*disabled: str):
        _patch_managed(monkeypatch, product_policy, lambda: _branded(*disabled))

    return _apply


@pytest.fixture
def stock(monkeypatch):
    _patch_managed(monkeypatch, product_policy, lambda: {})


# ==================== hermes update 的三个入口 ====================


def test_命令行入口在品牌构建里被拒(branded):
    branded("self_update")

    assert product_cli_gate.cli_command_allowed("update") is False
    message = product_cli_gate.cli_command_denial_message("update")
    assert "hermes update" in message
    # 只说拒绝不给去处，用户下一步就是来找我们问
    assert "应用内检查更新" in message


def test_命令行入口在stock里照常可用(stock):
    assert product_cli_gate.cli_command_allowed("update") is True


def test_斜杠命令在品牌构建里不可见也不可执行(branded):
    branded("self_update")

    assert hermes_commands.command_allowed("update") is False
    assert hermes_commands.command_allowed("/update") is False


def test_斜杠命令在stock里照常可用(stock):
    assert hermes_commands.command_allowed("update") is True


def test_只禁用别的能力时更新命令不受影响(branded):
    """闸门必须真的看能力名，而不是「只要是品牌构建就一律拒绝」。"""
    branded("moa")

    assert product_cli_gate.cli_command_allowed("update") is True
    assert hermes_commands.command_allowed("update") is True


@pytest.mark.parametrize(
    "command, capability",
    [("moa", "moa"), ("fallback", "provider_management"), ("portal", "nous_services")],
)
def test_其余命令行入口按各自能力位判定(branded, command, capability):
    """`/moa` 灰了但 `hermes moa configure` 能进 —— U5 之前真实存在的错位。"""
    branded(capability)
    assert product_cli_gate.cli_command_allowed(command) is False

    branded("pet_ai_generation")
    assert product_cli_gate.cli_command_allowed(command) is True


def test_不在表里的命令不受能力策略管辖(branded):
    branded("self_update", "moa", "nous_services")

    for command in ("chat", "status", "sessions", "version"):
        assert product_cli_gate.cli_command_allowed(command) is True


# ==================== 仪表盘 REST ====================


def _dashboard_rejects(capability: str) -> bool:
    from hermes_cli import web_server as ws

    try:
        ws._require_product_capability(capability)
    except ws.HTTPException as exc:
        return exc.status_code == 403 and exc.detail == "capability_unavailable"
    return False


def test_仪表盘更新与检查两个端点都被拒(branded):
    branded("self_update")
    assert _dashboard_rejects("self_update") is True


def test_仪表盘端点在stock里放行(stock):
    assert _dashboard_rejects("self_update") is False


def test_两个端点用的是同一个能力常量():
    """字面量拼错的后果是那一处静默放行，而这类失效不会有任何报错。"""
    source = (REPO_ROOT / "hermes_cli" / "web_server.py").read_text(encoding="utf-8")
    hits = re.findall(r"_require_product_capability\(SELF_UPDATE\)", source)

    assert len(hits) == 2, "应用与检查两个端点都必须过闸门"
    assert '_require_product_capability("self_update")' not in source


# ==================== Nous 官方内容与遥测通道 ====================


def test_技能索引在品牌构建里不去取(branded, monkeypatch):
    """nous_services 早就声明禁用了，只是这条通道一直没执行它。"""
    from tools import skills_hub

    def _explode(*args, **kwargs):
        raise AssertionError("品牌构建不该访问 Nous 文档站")

    monkeypatch.setattr(skills_hub.httpx, "get", _explode)
    branded("nous_services")

    assert skills_hub._load_hermes_index() is None


def test_同步面地址在品牌构建里解析不出来(branded):
    from tools import skills_sync_client

    branded("nous_services")
    assert skills_sync_client.resolve_sync_base_url() is None


def test_设了环境变量也绕不过同步面封堵(branded, monkeypatch):
    """能设环境变量的正是我们要挡的那个人，所以闸门必须排在读环境变量之前。"""
    from tools import skills_sync_client

    monkeypatch.setenv("HERMES_SYNC_BASE_URL", "https://gateway-gateway.nousresearch.com")
    branded("nous_services")

    assert skills_sync_client.resolve_sync_base_url() is None


def test_同步面在stock里仍指向官方(stock, monkeypatch):
    from tools import skills_sync_client

    monkeypatch.delenv("HERMES_SYNC_BASE_URL", raising=False)

    # 不打桩 load_config：stock 下就算读到空配置，也该回落到官方默认地址。
    # 断言「非空」而不是断言等于某个常量 —— 上游改默认地址是它的自由，
    # 我们要钉住的是「stock 仍然能解析出一个地址」。
    assert skills_sync_client.resolve_sync_base_url() is not None


def test_诊断日志在品牌构建里不外发(branded, monkeypatch):
    """三条 Nous 通道里后果最重的一条 —— 它外发的是日志。"""
    from hermes_cli import diagnostics_upload

    def _explode(*args, **kwargs):
        raise AssertionError("品牌构建不该把日志传给 Nous")

    monkeypatch.setattr(diagnostics_upload, "request_upload_url", _explode)
    branded("nous_services")

    with pytest.raises(RuntimeError) as excinfo:
        diagnostics_upload.share_to_nous(b"gzipped-bundle")

    assert "--local" in str(excinfo.value)


def test_诊断日志在stock里照常走上传(stock, monkeypatch):
    from hermes_cli import diagnostics_upload

    calls = []
    monkeypatch.setattr(
        diagnostics_upload, "request_upload_url",
        lambda **kwargs: calls.append(kwargs) or {"uploadUrl": "https://x", "viewUrl": "https://y"},
    )
    monkeypatch.setattr(diagnostics_upload, "put_bundle", lambda *a, **k: None)

    assert diagnostics_upload.share_to_nous(b"bundle")["viewUrl"] == "https://y"
    assert calls, "stock Hermes 必须照常上传，否则我们弄坏的是上游功能"


# ==================== Electron 侧：钉住已改好的重定向 ====================
#
# 这几条是**源码断言**，不是行为测试。理由是它们要防的事情发生在月度上游同步：
# 一次冲突解决把 CUSTOM 块还原成上游版本，桌面端就又能 git pull 官方仓库了，
# 而任何行为测试都不会红 —— 因为那时它跑的是上游的正确行为。

MAIN_TS = REPO_ROOT / "apps" / "desktop" / "electron" / "main.ts"


def _main_ts() -> str:
    return MAIN_TS.read_text(encoding="utf-8")


def test_更新检查走公司清单而不是官方源():
    source = _main_ts()
    handler = source[source.index("'hermes:updates:check'"):]
    handler = handler[: handler.index("ipcMain.handle(", 10)]

    assert "checkProductUpdate" in handler
    # 清单地址不再在这里写死：checkProductUpdate 默认就取
    # PRODUCT_POLICY.updateManifestUrl，参数只在测试里覆盖。
    # 真正要钉住的是"这个处理器不去碰 git"，见下面那条。
    assert "git" not in handler


def test_应用更新只走验签过的公司通道():
    """U3 之前这里恒返回不可用；现在它真的会装东西，要钉的东西也就变了。

    变成了两条：① 走的是 applyProductUpdate（内含验签、限长、SHA-256、
    交接），② 那条 git 老路一个字都不许出现在这个处理器里。
    """
    source = _main_ts()
    handler = source[source.index("'hermes:updates:apply'"):]
    handler = handler[: handler.index("// CUSTOM-END", 10)]

    assert "applyProductUpdate(" in handler
    for forbidden in ("applyUpdates(", "git", "NousResearch"):
        assert forbidden not in handler, f"apply 处理器里不该出现 {forbidden}"


def test_旧的git自更新实现已经没有IPC入口():
    """`applyUpdates` / `checkUpdates` 是上游那条 git 自更新路径。

    它们还留在文件里（上游代码，按定制纪律不删），但必须没有任何 IPC 处理器
    能走到。这条防的是月度同步把某个处理器改回去。
    """
    source = _main_ts()
    handlers_start = source.index("ipcMain.handle('hermes:updates:check'")
    handlers = source[handlers_start : source.index("ipcMain.handle('hermes:updates:branch:set'")]

    assert "applyUpdates(" not in handlers
    assert "checkUpdates(" not in handlers


def test_分支切换不再由git_ref决定():
    source = _main_ts()

    assert "'hermes:updates:branch:get'" in source
    assert "branch: 'managed'" in source


def test_官方仓库地址没有出现在桌面端更新处理器里():
    source = _main_ts()
    start = source.index("'hermes:updates:check'")
    end = source.index("ipcMain.handle('hermes:updates:branch:set'")

    assert "NousResearch" not in source[start:end]


# ==================== 闸门确实接在分发点上 ====================
#
# 上面所有命令行相关的用例证明的是「cli_command_allowed 算得对」。
# 那不等于「它接在 hermes 真正走的那条路上」——第一轮变异验证里，把 main.py
# 分发点上的闸门整段摘掉，前面 22 条测试全绿。
#
# 这是本项目第四次撞上「测函数不等于测到那条路」（2026-08-20 / 08-21 / 08-23）。
#
# 为什么在进程内跑 main() 而不是起子进程：起子进程更真实，但**闸门坏掉时那个
# 子进程会真的去 git pull 当前这棵源码树**——验证闸门的测试恰好是闸门坏掉时
# 毁掉仓库的那个测试。仓库的 conftest.py 里本来就有一道 live-system 守卫拦这件事，
# 它拦得对。进程内跑 + 把 cmd_update 换成会爆炸的替身，坏掉时炸的是断言而不是仓库。

import sys
from types import SimpleNamespace

from hermes_cli import main as hermes_main


def _patch_managed(monkeypatch, module, factory):
    """Branded/stock fixture for both managed-config seams.

    ``load_product_policy`` now reads the tri-state ``load_managed_config_status``
    so a broken managed install fails closed instead of looking stock; other
    helpers still read the legacy dict loader. Patch both from one factory.
    """
    from pathlib import Path

    from hermes_cli.managed_scope import ManagedConfigState, ManagedConfigStatus

    def status():
        config = factory()
        if not config:
            return ManagedConfigStatus(ManagedConfigState.ABSENT, {}, None)
        return ManagedConfigStatus(ManagedConfigState.VALID, config, Path("/managed"))

    monkeypatch.setattr(module, "load_managed_config", factory)
    monkeypatch.setattr(module, "load_managed_config_status", status)


def _drive_cli(monkeypatch, argv, *, reached):
    """跑真实的 main() 分发路径；子命令处理器换成记录调用的替身。"""
    def _record(name):
        def _handler(args):  # noqa: ANN001
            reached.append(name)
            return 0

        return _handler

    monkeypatch.setattr(hermes_main, "cmd_update", _record("update"), raising=False)
    monkeypatch.setattr(sys, "argv", ["hermes", *argv])

    try:
        hermes_main.main()
    except SystemExit as exc:
        return exc.code
    return 0


def test_更新命令走到分发点时被闸门挡下(branded, monkeypatch, capsys):
    """摘掉 main.py 分发点上的接线，只有这条会红。"""
    branded("self_update")
    reached: list[str] = []

    code = _drive_cli(monkeypatch, ["update"], reached=reached)
    output = capsys.readouterr().out

    assert code == 2
    assert reached == [], "闸门没拦住，请求已经走进 cmd_update"
    assert "hermes update" in output
    assert "应用内检查更新" in output


def test_stock下更新命令照常走进处理器(stock, monkeypatch, capsys):
    """挡多了等于把上游功能弄坏，和挡漏了一样是 bug。"""
    reached: list[str] = []

    code = _drive_cli(monkeypatch, ["update"], reached=reached)

    assert code == 0
    assert reached == ["update"]
    assert "在本产品中不可用" not in capsys.readouterr().out


def test_只禁用别的能力时更新命令照常走进处理器(branded, monkeypatch):
    """闸门必须真的看能力名，不是「品牌构建就一律拒绝」。"""
    branded("moa")
    reached: list[str] = []

    assert _drive_cli(monkeypatch, ["update"], reached=reached) == 0
    assert reached == ["update"]
