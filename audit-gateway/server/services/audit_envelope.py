"""Extract the private client audit envelope before upstream construction."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, List, Optional

AUDIT_ENVELOPE_KEY = "_llano_audit"
MAX_AUDIT_ENVELOPE_BYTES = 64 * 1024 * 1024

# 会话串联用的标识与轮次类型（决策 #52）。
#
# ⚠️ **这两个字段都是客户端填的，一律当不可信输入处理。** 客户端做过一次
# 归一化（agent/redaction_payload.py），但那是我们自己的构建里的一次「别发垃圾」，
# 不是安全边界 —— 谁都能手写一个 body 打过来。所以这里独立校验一遍，
# 而不是复用客户端的结论。三条硬规矩（决策 #52）：
#
# 1. **只用于把记录串起来看，绝不参与鉴权**
# 2. 串联**限定在同一 user_uid 内**（在查询侧强制，见 db/selects/audit.py）——
#    否则填别人的 session_id 就能把自己的记录挂到别人的会话下面
# 3. 不合规的 session_id 存 NULL；未知的 turn_kind 落白名单外的默认值，
#    别让客户端字符串变成无界枚举（那一列会长出运营根本没定义过的取值）
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

TURN_KIND_CHAT = "chat"
TURN_KIND_OTHER = "other"
TURN_KINDS = frozenset({TURN_KIND_CHAT, "title", "approval", TURN_KIND_OTHER})


class AuditEnvelopeError(ValueError):
    pass


@dataclass(frozen=True)
class AuditEnvelope:
    """一次调用里可供审计留存的部分"""

    messages: List[Any] = field(default_factory=list)
    # None 表示「串不起来」：stock 客户端、绕过我们客户端的直连调用，
    # 以及带了但没通过校验的。这类记录**必须照常出现在列表里**，
    # 退化成一行一条 —— 因为分不了组就藏起来，正好把最该看的那类调用藏了
    session_id: Optional[str] = None
    turn_kind: str = TURN_KIND_CHAT


def normalize_session_id(value: Any) -> Optional[str]:
    """按字符集与长度收敛客户端给的会话 ID，不合规返回 None"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if _SESSION_ID_RE.match(text) else None


def normalize_turn_kind(value: Any) -> str:
    """把客户端给的轮次类型收敛到白名单内，未知值一律 other"""
    if not isinstance(value, str):
        return TURN_KIND_OTHER
    text = value.strip().lower()
    return text if text in TURN_KINDS else TURN_KIND_OTHER


def extract_audit_envelope(body: dict[str, Any]) -> AuditEnvelope:
    """摘掉私有字段，返回这次调用要留存的审计内容

    没有信封时**退化成 chat 而不是 other**：那一档在列表页默认显示。
    未知来源的调用（绕过客户端的、stock Hermes 的）默认落进「默认隐藏」那一档，
    等于把最该被看见的调用藏起来 —— 方向反了。
    """
    received_messages = body.get("messages")
    envelope = body.pop(AUDIT_ENVELOPE_KEY, None)
    if envelope is None:
        return AuditEnvelope(
            messages=(
                deepcopy(received_messages)
                if isinstance(received_messages, list)
                else []
            )
        )

    if not isinstance(envelope, dict):
        raise AuditEnvelopeError("私有审计字段格式无效")
    version = envelope.get("policy_version")
    messages = envelope.get("messages")
    if not isinstance(version, str) or not version:
        raise AuditEnvelopeError("私有审计字段缺少策略版本")
    if not isinstance(messages, list) or not messages:
        raise AuditEnvelopeError("私有审计字段缺少原始消息")

    encoded_size = len(
        json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    if encoded_size > MAX_AUDIT_ENVELOPE_BYTES:
        raise AuditEnvelopeError("私有审计字段超过大小限制")

    # 串联字段**不合规就降级，不报错**：它们只影响审计怎么展示，
    # 让一次正常的对话因为会话 ID 里多了个点号而整个失败，是把
    # 「看得更清楚」这件事的代价转嫁给了用户
    return AuditEnvelope(
        messages=deepcopy(messages),
        session_id=normalize_session_id(envelope.get("session_id")),
        turn_kind=normalize_turn_kind(
            envelope.get("turn_kind", TURN_KIND_CHAT)
        ),
    )


def extract_audit_messages(body: dict[str, Any]) -> List[Any]:
    """旧名保留：只要消息、不关心串联字段的调用方仍可直接用"""
    return extract_audit_envelope(body).messages
