"""品牌版：公司网关上的模型能不能直接看图。

# CUSTOM（整份文件是品牌定制，上游没有）

## 为什么需要

Hermes 判断「主模型能不能直接收图片」靠 config 里的 ``supports_vision`` 覆盖项，
再查 models.dev。公司网关的模型（``deepseek-v4.1-flash``、豆包、MiMo）两处都查不到，
于是一律被当成**不能看图**：

- 用户贴的图先交给辅助视觉模型写成一段文字描述，主模型只看到这段描述
  （``agent/image_routing.decide_image_input_mode`` 走 ``text``）；
- 已经在消息里的图片部件被整段剥掉（``run_agent._model_supports_vision``）。

表现就是「DeepSeek 不支持直接发图片」—— 其实 DeepSeek V4.1 Flash 官方支持
``image_url``（https://api-docs.deepseek.com/zh-cn/guides/vision），网关后台也早就
给它勾了「支持多模态」，只是客户端从来没读这个开关。

网关的 ``/v1/models`` 现在给每个模型带 ``supports_vision``。这里只对**公司网关**
读它，别的 provider 行为不变。

## 工具结果里的图片

DeepSeek 只接受 ``user`` 消息里的图片，``tool``/``assistant`` 消息带图会 400；
MiMo 对列表形式的 tool 内容也回 400。网关背后挂着好几家上游，逐家区分不划算，
所以公司网关一律把工具结果里的截图降级为文字摘要（这正是之前的实际行为），
只有用户自己发的图直接给模型看。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

GATEWAY_API_KEY_ENV = "LLANO_GATEWAY_API_KEY"


def _active_policy():
    try:
        from hermes_cli.product_policy import load_product_policy

        return load_product_policy()
    except Exception:  # noqa: BLE001 — 读不到策略就当 stock，不影响上游行为
        return None


def is_company_gateway(provider: str, base_url: str) -> bool:
    """当前 provider/base_url 是不是品牌版的公司网关。stock 安装恒为 False。"""
    policy = _active_policy()
    if policy is None:
        return False
    try:
        from agent.redaction_policy import _is_managed_endpoint

        return _is_managed_endpoint(provider, base_url, policy)
    except Exception:  # noqa: BLE001
        return False


def _gateway_base_url(base_url: str) -> str:
    if base_url:
        return base_url
    policy = _active_policy()
    origin = str(getattr(policy, "gateway_origin", "") or "").rstrip("/")
    return f"{origin}/v1" if origin else ""


def gateway_model_supports_vision(provider: str, model: str, base_url: str) -> Optional[bool]:
    """公司网关对这个模型声明的看图能力；不是公司网关或网关没说时返回 None。

    None 让调用方照旧走 models.dev 等上游逻辑 —— 老网关不发这个字段时，
    行为和改之前完全一样。
    """
    if not model or not is_company_gateway(provider, base_url):
        return None

    resolved = _gateway_base_url(base_url)
    if not resolved:
        return None

    try:
        from agent.model_metadata import fetch_endpoint_model_metadata

        # 结果按 base_url 缓存 300 秒，启动时算上下文窗口那次已经填好了。
        metadata: Dict[str, Dict[str, Any]] = fetch_endpoint_model_metadata(
            resolved, api_key=os.environ.get(GATEWAY_API_KEY_ENV, "")
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("product_vision: gateway metadata lookup failed: %s", exc)
        return None

    entry = metadata.get(model)
    if not isinstance(entry, dict):
        return None
    value = entry.get("supports_vision")
    return value if isinstance(value, bool) else None
