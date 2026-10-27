"""阶段 4 · 安全绕过矩阵：每个能力 × 每条入口通道，逐格断言。

## 为什么要有这个文件

原先的策略测试是「一事一例」散着写的：某个能力在某条通道上被拒了，就补一个用例。
这种写法防不住两类事故，而这两类恰恰是最可能发生的：

1. **上游重构出一条新入口**。同步 upstream 之后多了一个 REST 路由或 RPC 方法，
   旧用例全绿，新入口无人把守 —— 直到有人从那儿绕出去。
2. **我们自己加了第 8 个能力**，却只在 `product-environments.json` 里登记，
   忘了接执行层。同样全绿。

所以这里换成**表驱动 + 完备性元测试**：

- `MATRIX` 把每个 (能力, 通道) 显式声明成两种状态之一 —— `enforced`（探针必须拒绝）
  或 `not_applicable`（**必须写理由**）。没有第三种状态。
- `test_matrix_covers_every_shipped_capability` 直接读
  `apps/desktop/product-environments.json`：产品里禁用了但矩阵里没有的能力，直接失败。
- `test_matrix_covers_every_declared_channel` 反向查：新增通道忘了填格子也会失败。

**「不适用」不是免责声明，是一条需要理由的断言。** 写 `not_applicable` 时如果讲不出
「这条通道结构上碰不到这个能力」，那它就该是 `enforced`。

## 与 stock Hermes 的关系

每个探针都成对跑：品牌策略下必须拒绝，无托管策略（stock Hermes）下必须放行。
只测前者的话，一个「把所有人都拒了」的实现也能全绿，而那会把上游功能一起弄坏。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import commands as hermes_commands
from hermes_cli import product_policy
from hermes_cli import web_server as ws
from tui_gateway import server as tui_server


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

REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCT_ENVIRONMENTS = REPO_ROOT / "apps" / "desktop" / "product-environments.json"

ENFORCED = "enforced"
NOT_APPLICABLE = "not_applicable"


def _branded(*capabilities: str):
    """构造一份只禁用指定能力的品牌策略。

    逐个能力单独禁用（而不是一次全禁），这样某条通道如果实现成
    「只要是品牌构建就一律拒绝」而没真的看能力名，也会被抓出来。
    """
    return {
        "product": {
            "enabled": True,
            "brand_id": "llano-ai-workbench",
            "provider_id": "llano-gateway",
            "disabled_capabilities": list(capabilities),
            "allowed_messaging_platforms": ["telegram", "weixin", "dingtalk"],
        }
    }


# ==================== 探针 ====================
# 每个探针回答同一个问题：这条通道现在会不会拒绝这个能力。
# 返回 True = 拒绝。探针只做判定，不打印、不做断言 —— 断言统一在下面的用例里，
# 这样失败信息里能同时看到能力名和通道名。
#
# 探针统一收 `monkeypatch`：有些通道（Nous 工具网关）在策略之外还有别的失败原因
# （没登录、没 Key），不把那些原因先支开的话，探针在放行时也返回「拒绝」，
# 就分不清到底是策略拦的还是环境缺东西 —— 那样的探针等于没有。


def _probe_config_write(path: str):
    def probe(monkeypatch) -> bool:
        return not product_policy.config_write_allowed(path)

    return probe


def _probe_config_sanitize(seed: dict, missing_path: str):
    def probe(monkeypatch) -> bool:
        sanitized = product_policy.sanitize_product_config(seed)
        node = sanitized
        for part in missing_path.split("."):
            if not isinstance(node, dict) or part not in node:
                return True
            node = node[part]
        return not node

    return probe


def _probe_rest(capability: str):
    def probe(monkeypatch) -> bool:
        try:
            ws._require_product_capability(capability)
        except ws.HTTPException as exc:
            return exc.status_code == 403 and exc.detail == "capability_unavailable"
        return False

    return probe


def _probe_rpc(capability: str):
    def probe(monkeypatch) -> bool:
        response = tui_server._capability_error("rid", capability)
        return (
            response is not None
            and response["error"]["data"]["reason"] == "capability_unavailable"
        )

    return probe


def _probe_slash(command: str):
    def probe(monkeypatch) -> bool:
        return not hermes_commands.command_allowed(command)

    return probe


def _probe_cli(command: str):
    """`hermes <command>` 这条命令行入口会不会被拒。

    这条通道是阶段 5 · U5 补上的。此前矩阵里没有它，于是出现过这样的错位：
    `/moa` 在命令面板里是灰的，同一台机器上敲 `hermes moa configure` 却直接进去。
    我们的产品自带终端工具，命令行不是「高级用户才碰得到的东西」。
    """

    def probe(monkeypatch) -> bool:
        from hermes_cli import product_cli_gate

        return not product_cli_gate.cli_command_allowed(command)

    return probe


def _probe_tool_image_generation():
    """被拒时返回的是 `{"error": "capability_unavailable"}` 这一串精确内容。

    这里必须**精确比对**而不是子串匹配：没配 FAL_KEY 时的「后端不可用」长报错里
    也会捎带出现 capability_unavailable 字样（它把各路不满足的条件逐条列了出来），
    子串匹配会把「环境没装好」误判成「策略拦住了」，两个完全不同的结论。
    """

    def probe(monkeypatch) -> bool:
        from tools import image_generation_tool

        raw = image_generation_tool._handle_image_generate({"prompt": "x"})
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return False
        return payload.get("error") == "capability_unavailable"

    return probe


def _probe_tool_nous_gateway():
    """先把「已登录且有权益」这个前提坐实，再看策略拦不拦。

    不先支开的话，stock Hermes 下这个函数同样返回 False（本机没登录 Nous），
    探针在两种策略下给出一样的答案，就证明不了任何事。
    """

    def probe(monkeypatch) -> bool:
        from types import SimpleNamespace

        from hermes_cli import nous_account
        from tools import tool_backend_helpers

        monkeypatch.setattr(
            nous_account,
            "get_nous_portal_account_info",
            lambda *args, **kwargs: SimpleNamespace(
                logged_in=True, tool_gateway_entitled=True
            ),
        )
        return tool_backend_helpers.managed_nous_tools_enabled() is False

    return probe


# ==================== 矩阵 ====================
# 通道清单。新增一条通道要同时在每个能力下填格子，否则完备性元测试失败。

CHANNELS = (
    "config_write",      # 写 config.yaml
    "config_sanitize",   # 读入旧配置后的净化（防「旧配置复活」）
    "rest",              # 仪表盘 REST
    "rpc",               # TUI / 桌面端 JSON-RPC
    "slash",             # slash 命令的发现与执行
    "tool_runtime",      # 模型工具执行层
    "cli",               # `hermes <子命令>` 命令行入口（产品自带终端，这是真入口）
)

# 每格：(状态, 探针或理由)
MATRIX: dict[str, dict[str, tuple]] = {
    "provider_management": {
        "config_write": (ENFORCED, _probe_config_write("model.provider")),
        "config_sanitize": (
            ENFORCED,
            _probe_config_sanitize({"model": {"base_url": "http://evil"}}, "model.base_url"),
        ),
        "rest": (ENFORCED, _probe_rest("provider_management")),
        "rpc": (
            NOT_APPLICABLE,
            "RPC 侧的模型切换不按能力名判定，而是「品牌构建一律锁死在托管 provider 上」，"
            "与是否禁用 provider_management 无关。它是另一种形状的约束，"
            "单独在下面的「供应商锁定」专章断言，塞进本矩阵会得出错误结论",
        ),
        "slash": (
            NOT_APPLICABLE,
            "没有切换供应商的 slash 命令；模型切换只走 RPC，由供应商锁定专章覆盖",
        ),
        "tool_runtime": (
            NOT_APPLICABLE,
            "供应商配置不是模型可调用的工具，工具执行层结构上碰不到这个能力",
        ),
        "cli": (ENFORCED, _probe_cli("fallback")),
    },
    "custom_model_providers": {
        "config_write": (ENFORCED, _probe_config_write("custom_providers")),
        "config_sanitize": (
            ENFORCED,
            _probe_config_sanitize({"providers": {"evil": {"api": "http://evil"}}}, "providers"),
        ),
        "rest": (ENFORCED, _probe_rest("custom_model_providers")),
        "rpc": (
            NOT_APPLICABLE,
            "自定义接入点的增删改只有 REST 入口，RPC 侧没有对应方法可绕",
        ),
        "slash": (
            NOT_APPLICABLE,
            "没有增删自定义接入点的 slash 命令，命令表里不存在该入口",
        ),
        "tool_runtime": (
            NOT_APPLICABLE,
            "接入点配置不是模型可调用的工具，工具执行层碰不到",
        ),
        "cli": (
            NOT_APPLICABLE,
            "没有 `hermes providers` 这样的子命令 —— 自定义接入点只能改 config.yaml "
            "或走 REST，两条都已在上面的格子里覆盖（已核对 main.py 的子命令表）",
        ),
    },
    "provider_oauth": {
        "config_write": (
            NOT_APPLICABLE,
            "OAuth 凭据不落 config.yaml，走各 provider 自己的凭据存储，配置路径里没有它",
        ),
        "config_sanitize": (
            NOT_APPLICABLE,
            "同 config_write：配置里根本没有对应字段，没有可净化的目标",
        ),
        "rest": (ENFORCED, _probe_rest("provider_oauth")),
        "rpc": (
            NOT_APPLICABLE,
            "OAuth 登录流程需要浏览器回调，只有 REST 入口，RPC 侧无此方法",
        ),
        "slash": (
            NOT_APPLICABLE,
            "没有发起 provider OAuth 的 slash 命令，命令表里不存在该入口",
        ),
        "tool_runtime": (
            NOT_APPLICABLE,
            "OAuth 登录不是模型可调用的工具，工具执行层碰不到",
        ),
        "cli": (
            NOT_APPLICABLE,
            "OAuth 登录需要浏览器回调，只有 REST 入口，命令行侧没有对应子命令",
        ),
    },
    "nous_services": {
        "config_write": (ENFORCED, _probe_config_write("nous.portal")),
        "config_sanitize": (
            ENFORCED,
            _probe_config_sanitize({"nous": {"portal": {"token": "x"}}}, "nous"),
        ),
        "rest": (ENFORCED, _probe_rest("nous_services")),
        "rpc": (ENFORCED, _probe_rpc("nous_services")),
        "slash": (ENFORCED, _probe_slash("subscription")),
        "tool_runtime": (ENFORCED, _probe_tool_nous_gateway()),
        "cli": (ENFORCED, _probe_cli("portal")),
    },
    "moa": {
        "config_write": (ENFORCED, _probe_config_write("moa.enabled")),
        "config_sanitize": (
            ENFORCED,
            _probe_config_sanitize({"moa": {"enabled": True}}, "moa"),
        ),
        "rest": (ENFORCED, _probe_rest("moa")),
        "rpc": (
            NOT_APPLICABLE,
            "MOA 没有独立 RPC 方法；它经模型切换生效，由供应商锁定专章一并兜住",
        ),
        "slash": (ENFORCED, _probe_slash("moa")),
        "tool_runtime": (
            NOT_APPLICABLE,
            "MOA 是多模型编排模式，不是模型可调用的工具，工具执行层碰不到",
        ),
        "cli": (ENFORCED, _probe_cli("moa")),
    },
    "builtin_image_generation": {
        "config_write": (ENFORCED, _probe_config_write("image_gen.backend")),
        "config_sanitize": (
            ENFORCED,
            _probe_config_sanitize({"image_gen": {"backend": "fal"}}, "image_gen"),
        ),
        "rest": (
            NOT_APPLICABLE,
            "内置图片生成没有 REST 入口，只能由模型作为工具调用，走 tool_runtime",
        ),
        "rpc": (
            NOT_APPLICABLE,
            "同 rest：图片生成不是 RPC 方法，RPC 侧没有可绕的入口",
        ),
        "slash": (
            NOT_APPLICABLE,
            "没有触发图片生成的 slash 命令，命令表里不存在该入口",
        ),
        "tool_runtime": (ENFORCED, _probe_tool_image_generation()),
        "cli": (
            NOT_APPLICABLE,
            "没有触发图片生成的子命令；它只能由模型作为工具调用，走 tool_runtime",
        ),
    },
    "pet_ai_generation": {
        "config_write": (ENFORCED, _probe_config_write("pet.generation")),
        "config_sanitize": (
            ENFORCED,
            _probe_config_sanitize({"pet": {"generation": True}}, "pet.generation"),
        ),
        "rest": (
            NOT_APPLICABLE,
            "Pet 生成没有独立 REST 路由，入口在命令面板与 RPC，已由那两格覆盖",
        ),
        "rpc": (ENFORCED, _probe_rpc("pet_ai_generation")),
        "slash": (ENFORCED, _probe_slash("hatch")),
        "tool_runtime": (
            NOT_APPLICABLE,
            "Pet 生成不是模型可调用的工具，工具执行层结构上碰不到",
        ),
        "cli": (
            NOT_APPLICABLE,
            "`hermes pets` 只有 list/install/select/show/off/scale/remove/doctor —— "
            "装的是 petdex 上的预制精灵，没有生成子命令（已核对 hermes_cli/pets.py）",
        ),
    },
    # 阶段 5 · U5：自有更新闭环之后，官方 git / GitHub ZIP 更新通道必须不可达
    "self_update": {
        "config_write": (
            NOT_APPLICABLE,
            "更新通道不由用户配置决定：清单地址来自托管配置的 product.update_manifest_url，"
            "user 侧 config.yaml 里没有可写到这条路上的字段",
        ),
        "config_sanitize": (
            NOT_APPLICABLE,
            "同 config_write：配置里根本没有对应字段，没有可净化、可复活的目标",
        ),
        "rest": (ENFORCED, _probe_rest("self_update")),
        "rpc": (
            NOT_APPLICABLE,
            "桌面端的更新走 Electron IPC（hermes:updates:*），不是 TUI 的 JSON-RPC。"
            "那条 IPC 已经被改成公司网关且 apply 恒为 fail-closed，"
            "由 test_product_update_channel.py 的回归用例单独钉住 —— "
            "塞进本矩阵会得出「RPC 挡住了」这个错误结论",
        ),
        "slash": (ENFORCED, _probe_slash("update")),
        "tool_runtime": (
            NOT_APPLICABLE,
            "更新不是模型可调用的工具，工具执行层结构上碰不到",
        ),
        "cli": (ENFORCED, _probe_cli("update")),
    },
}


def _cells():
    for capability, channels in MATRIX.items():
        for channel, (state, payload) in channels.items():
            yield capability, channel, state, payload


# ==================== 完备性：矩阵自己不能有洞 ====================


def test_matrix_covers_every_shipped_capability():
    """产品里禁用了、矩阵里没有的能力 —— 直接失败。

    这条是整个文件的立身之本：它把「加了能力忘了接执行层」从
    「下次同步上游才发现」提前到「改 product-environments.json 当天就发现」。
    """
    shipped = set(json.loads(PRODUCT_ENVIRONMENTS.read_text())["disabledCapabilities"])
    missing = shipped - set(MATRIX)

    assert not missing, (
        f"{PRODUCT_ENVIRONMENTS.name} 里禁用了这些能力但矩阵没覆盖：{sorted(missing)}。"
        "补上对应的行，每条通道要么给探针，要么写明为什么不适用"
    )


def test_matrix_covers_every_declared_channel():
    """新增通道忘了填格子 —— 直接失败。"""
    for capability, channels in MATRIX.items():
        missing = set(CHANNELS) - set(channels)
        assert not missing, f"能力 {capability} 缺这些通道的格子：{sorted(missing)}"

        unknown = set(channels) - set(CHANNELS)
        assert not unknown, f"能力 {capability} 填了未声明的通道：{sorted(unknown)}"


def test_every_not_applicable_cell_states_a_reason():
    """`not_applicable` 必须带理由，且不能是敷衍的一两个字。

    没有理由的「不适用」和没写是一回事 —— review 时看不出它是想清楚了还是漏了。
    """
    for capability, channel, state, payload in _cells():
        if state != NOT_APPLICABLE:
            continue
        assert isinstance(payload, str) and len(payload.strip()) >= 10, (
            f"{capability} × {channel} 标了不适用但没写清理由：{payload!r}"
        )


def test_matrix_states_are_valid():
    for capability, channel, state, _payload in _cells():
        assert state in (ENFORCED, NOT_APPLICABLE), (
            f"{capability} × {channel} 的状态 {state!r} 非法，只能是 enforced / not_applicable"
        )


# ==================== 逐格断言 ====================

_ENFORCED_CELLS = [
    pytest.param(capability, channel, probe, id=f"{capability}::{channel}")
    for capability, channel, state, probe in _cells()
    if state == ENFORCED
]


@pytest.mark.parametrize("capability,channel,probe", _ENFORCED_CELLS)
def test_branded_build_rejects_capability_on_channel(
    capability, channel, probe, monkeypatch
):
    """品牌构建下，这条通道必须拒绝这个能力。"""
    _patch_managed(monkeypatch, product_policy, lambda: _branded(capability))

    assert probe(monkeypatch) is True, (
        f"能力 {capability} 在通道 {channel} 上没有被拒绝 —— 这是一条可绕过的口子"
    )


@pytest.mark.parametrize("capability,channel,probe", _ENFORCED_CELLS)
def test_stock_hermes_keeps_capability_on_channel(
    capability, channel, probe, monkeypatch
):
    """stock Hermes（无托管策略）下，同一条通道必须照常放行。

    少了这一半，一个「无差别全拒」的实现也能让上面那组全绿，
    而那等于把上游功能一起废掉。
    """
    _patch_managed(monkeypatch, product_policy, lambda: {})

    assert probe(monkeypatch) is False, (
        f"stock Hermes 下 {capability} × {channel} 被拒了 —— 品牌策略泄漏到了上游行为"
    )


@pytest.mark.parametrize("capability,channel,probe", _ENFORCED_CELLS)
def test_other_capabilities_stay_untouched(capability, channel, probe, monkeypatch):
    """只禁用**别的**能力时，这条通道必须放行。

    防的是「一刀切」实现：某条通道只判断「是不是品牌构建」，不看具体能力名。
    那种实现在真实产品里看不出问题（因为 7 个能力全禁了），
    但它意味着策略是假的 —— 将来想单独放开一个能力会发现根本放不开。
    """
    others = [name for name in MATRIX if name != capability]
    _patch_managed(monkeypatch, product_policy, lambda: _branded(*others))

    assert probe(monkeypatch) is False, (
        f"只禁用了其他能力，{capability} × {channel} 却被拒 —— "
        "该通道没有按能力名判定，是一刀切实现"
    )


# ==================== 供应商锁定（形状不同，单独立章） ====================
# 这一组不属于上面的能力矩阵：它不按能力名开关，而是「只要是品牌构建，
# 就只能用托管 provider 的模型」。阶段 4 第 2 条点名要覆盖的「自定义 provider 绕过」
# 落在这里，而不是 provider_management 那一行。


def test_branded_build_pins_provider_regardless_of_capability_flags(monkeypatch):
    """即使一个能力都没禁用，品牌构建照样不许跨出托管 provider。

    这条容易被误解成「provider_management 没禁用就能换供应商」——
    实际上供应商锁定是品牌构建的固有约束，和能力开关是两套东西。
    """
    _patch_managed(monkeypatch, product_policy, lambda: _branded())

    assert product_policy.model_switch_allowed() is True, "换本网关内的模型应当允许"
    assert product_policy.model_switch_allowed("llano-gateway") is True
    assert product_policy.model_switch_allowed("openrouter") is False
    assert product_policy.model_switch_allowed("anthropic") is False
    assert product_policy.model_switch_allowed("custom") is False


def test_provider_lock_is_case_insensitive(monkeypatch):
    """`LLANO-Gateway` 这种大小写变体不能成为绕过手段。"""
    _patch_managed(monkeypatch, product_policy, lambda: _branded())

    assert product_policy.model_switch_allowed("LLANO-GATEWAY") is True
    assert product_policy.model_switch_allowed("  llano-gateway  ") is True


def test_stock_hermes_keeps_provider_freedom(monkeypatch):
    _patch_managed(monkeypatch, product_policy, lambda: {})

    assert product_policy.model_switch_allowed("openrouter") is True
    assert product_policy.model_switch_allowed("anthropic") is True
