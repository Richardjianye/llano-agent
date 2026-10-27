"""辅助调用（标题生成、命令审批扫描……）在托管端点上的脱敏与审计信封。

⚠️ **这些用例必须打 `_build_call_kwargs`，而不是只打
`apply_managed_audit_envelope`。** 2026-08-20 和 08-21 各栽过一次同样的跟头
（见 TODO 的「第 1 格测错了故障」「同一句英文漏了两次」）：**测函数不等于
测到那条路**。辅助调用的真正问题从来不是「这个纯函数算得对不对」，
而是「它到底有没有接在辅助调用真正走的那条路上」。
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.auxiliary_client import _build_call_kwargs
from agent.aux_redaction import (ManagedEndpointRequired,
                                 turn_kind_for_task)
from agent.redaction_policy import (RedactionPolicy, RedactionPolicyCache,
                                    RedactionPolicyUnavailable)
from hermes_cli.product_policy import ProductPolicyState, ProductPolicyStatus

GATEWAY = "https://gateway.example"
PRODUCT = SimpleNamespace(provider_id="llano-gateway", gateway_origin=GATEWAY)


POLICY = RedactionPolicy(
    version="sha256:v1",
    keywords=("示例公司", "示例公司"),
    placeholder="[公司]",
    expires_at=datetime.now(timezone.utc) + timedelta(days=1),
)


def _client(base_url=f"{GATEWAY}/v1", api_key="device-secret"):
    return SimpleNamespace(base_url=base_url, api_key=api_key)


def _build(*, provider="custom", base_url=f"{GATEWAY}/v1", task="title_generation",
           client=None, messages=None):
    return _build_call_kwargs(
        provider,
        "flash",
        messages or [{"role": "user", "content": "给「示例公司」的这段对话起个标题"}],
        timeout=30.0,
        base_url=base_url,
        task=task,
        client=client if client is not None else _client(base_url=base_url),
    )


@contextmanager
def _managed(*, available=True):
    """装上品牌策略，并把策略缓存换成一个不碰网络的替身。

    ⚠️ 不能用 patch 换掉 `agent.redaction_policy._POLICY_CACHE` 或 `_fetch_json`：
    `resolve_product_redaction_policy` 把这两个都写成了**默认参数值**，
    在 def 那一刻就绑死了，之后改模块属性对它毫无影响。所以这里换的是
    `RedactionPolicyCache.resolve` 这个方法本身 —— 不管用的是哪个实例都拦得住。
    这样 `_is_managed_endpoint` 那段真实判定仍然在跑，被替掉的只有取策略这一步。
    """
    def resolve(_self, **_kwargs):
        if not available:
            raise RedactionPolicyUnavailable("redaction policy unavailable")
        return POLICY

    with (
        patch(
            "hermes_cli.product_policy.load_product_policy_status",
            return_value=ProductPolicyStatus(
                ProductPolicyState.ACTIVE,
                policy=PRODUCT,
            ),
        ),
        patch.object(RedactionPolicyCache, "resolve", resolve),
    ):
        yield


# ==================== stock 构建不受影响 ====================


def test_stock_hermes_auxiliary_calls_are_untouched():
    """没有品牌策略就什么都不做 —— 上游行为一个字节都不能变。"""
    with patch(
        "hermes_cli.product_policy.load_product_policy_status",
        return_value=ProductPolicyStatus(ProductPolicyState.STOCK),
    ):
        kwargs = _build()

    assert "extra_body" not in kwargs or "_llano_audit" not in kwargs.get("extra_body", {})
    assert kwargs["messages"][0]["content"].startswith("给「示例公司」")


# ==================== 托管端点：脱敏 + 信封 ====================


def test_managed_auxiliary_call_is_redacted_and_carries_the_envelope():
    with _managed():
        kwargs = _build()

    envelope = kwargs["extra_body"]["_llano_audit"]
    # 发出去的那份脱敏了
    assert "示例公司" not in kwargs["messages"][0]["content"]
    assert "[公司]" in kwargs["messages"][0]["content"]
    # 审计那份是原文
    assert "示例公司" in envelope["messages"][0]["content"]
    assert envelope["policy_version"] == "sha256:v1"


def test_managed_auxiliary_call_is_tagged_with_the_active_session():
    """标题生成不该在审计里当无名氏 —— 它属于某一次会话。

    session_id 从 aux_accounting 那个 ContextVar 读，不新铺一条参数链路。
    """
    from agent import aux_accounting

    token = aux_accounting.set_accounting_context(
        SimpleNamespace(), "20260821_151706_ac236f"
    )
    try:
        with _managed():
            kwargs = _build(task="title_generation")
    finally:
        aux_accounting.reset_accounting_context(token)

    envelope = kwargs["extra_body"]["_llano_audit"]
    assert envelope["session_id"] == "20260821_151706_ac236f"
    assert envelope["turn_kind"] == "title"


def test_outside_a_turn_the_call_still_goes_out_just_unlinked():
    """没有会话上下文不是错误，只是串不起来。"""
    with _managed():
        kwargs = _build(task="approval")

    envelope = kwargs["extra_body"]["_llano_audit"]
    assert "session_id" not in envelope
    assert envelope["turn_kind"] == "approval"


# ==================== 品牌构建：辅助调用不许离开公司网关 ====================


def test_branded_build_refuses_to_send_an_auxiliary_call_off_the_gateway():
    """负责人 2026-08-23 拍板方法 A：不是「脱敏后放它走」，是**不许走**。

    辅助调用的降级链（任务配置里的 fallback_chain）可以指向别的 provider。
    落在那儿的请求既没脱敏也没审计 —— 本模块补完其余口子之后，
    它是这一类里仅剩的那个。选择拒发而不是「给第三方端点也脱一遍敏」，
    是为了只保留**一条**规矩（品牌流量只去公司网关，没有例外）而不是两条，
    也和阶段 2 的渠道收敛同向。配置仍是第一道防线，这里是配置漂了以后的兜底。
    """
    with _managed():
        with pytest.raises(ManagedEndpointRequired):
            _build(
                provider="openrouter",
                base_url="https://openrouter.ai/api/v1",
                client=_client(base_url="https://openrouter.ai/api/v1"),
            )


def test_the_refusal_names_the_destination_so_config_drift_is_diagnosable():
    """报错要能直接看出「它想去哪」，否则排查得从降级链倒着猜。"""
    with _managed():
        with pytest.raises(ManagedEndpointRequired, match="openrouter"):
            _build(
                provider="openrouter",
                base_url="https://openrouter.ai/api/v1",
                client=_client(base_url="https://openrouter.ai/api/v1"),
            )


# ==================== 托管端点上策略拿不到 → 不发 ====================


def test_managed_endpoint_without_a_policy_refuses_to_send():
    """fail-closed，和主循环（决策 #9）保持同一条规矩。

    代价接近于零：策略拿不到意味着网关连不上或凭证失效，
    此时主对话本来就已经在报错。审批扫描抛错会升级给人工
    （tools/approval.py），标题生成抛错只是没标题。
    """
    with _managed(available=False):
        with pytest.raises(RedactionPolicyUnavailable):
            _build()


# ==================== turn_kind 映射 ====================


@pytest.mark.parametrize(
    "task, expected",
    [
        ("title_generation", "title"),
        ("approval", "approval"),
        ("vision", "other"),
        ("compression", "other"),
        ("session_search", "other"),
        (None, "other"),
        ("", "other"),
    ],
)
def test_auxiliary_tasks_map_onto_the_audit_vocabulary(task, expected):
    """认不出的任务一律 other —— 网关那边也是白名单，加在这里没意义。"""
    assert turn_kind_for_task(task) == expected
