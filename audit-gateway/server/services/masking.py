"""发送方向的关键词脱敏兜底（决策 #9）

定位要说清楚：这是**聊胜于无的防御纵深**，不是匿名化。项目代号、黑话、
写作风格都能反向识别，主防线是与厂商签的书面数据协议。

只处理发送方向：
- 出网前把公司名/品牌词替换掉
- 接收方向只存不改，保证审计看到的就是模型真实回复

关键词从配置读（MASK_KEYWORDS，逗号分隔），替换成 MASK_PLACEHOLDER。
客户端本地也会做一遍，这里是服务端兜底——两道都做是因为客户端可能被绕过。
"""

from __future__ import annotations

import base64
import binascii
import re
from functools import lru_cache
from typing import List, Tuple
from urllib.parse import quote_from_bytes, unquote_to_bytes

DEFAULT_PLACEHOLDER = "[公司]"
_DATA_URL_RE = re.compile(r"^data:([^;,]*)(;[^,]*)?,(.*)$", re.DOTALL)
_SAFE_TEXT_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "text/csv",
        "text/markdown",
        "text/plain",
        "text/tab-separated-values",
        "text/xml",
    }
)


@lru_cache
def _compiled_pattern() -> Tuple[re.Pattern | None, str]:
    from config import get_settings
    from services.redaction_policy import normalized_keywords

    settings = get_settings()
    keywords = normalized_keywords(settings.mask_keywords or "")
    placeholder = settings.mask_placeholder or DEFAULT_PLACEHOLDER

    if not keywords:
        return None, placeholder

    pattern = re.compile("|".join(re.escape(k) for k in keywords), re.IGNORECASE)
    return pattern, placeholder


def reset_cache() -> None:
    """改配置或测试时调用"""
    _compiled_pattern.cache_clear()


def mask_text(text: str) -> Tuple[str, bool]:
    """返回 (脱敏后文本, 是否发生过替换)"""
    if not text:
        return text, False

    pattern, placeholder = _compiled_pattern()
    if pattern is None:
        return text, False

    masked, count = pattern.subn(placeholder, text)
    return masked, count > 0


def _mask_text_data_url(value: str) -> Tuple[str, bool]:
    """Mask UTF-8 text attachments while leaving binary/complex files untouched."""
    match = _DATA_URL_RE.match(value.strip())
    if not match:
        return value, False

    mime = (match.group(1) or "").strip().lower()
    params = match.group(2) or ""
    encoded = match.group(3)
    if mime not in _SAFE_TEXT_MIME_TYPES:
        return value, False

    try:
        if "base64" in params.lower():
            cleaned = re.sub(r"\s+", "", encoded)
            raw = base64.b64decode(cleaned + "=" * (-len(cleaned) % 4), validate=False)
        else:
            raw = unquote_to_bytes(encoded)
        text = raw.decode("utf-8")
        masked, hit = mask_text(text)
        if not hit:
            return value, False
        masked_bytes = masked.encode("utf-8")
        if "base64" in params.lower():
            replacement = base64.b64encode(masked_bytes).decode("ascii")
        else:
            replacement = quote_from_bytes(masked_bytes, safe="")
        return f"data:{match.group(1)}{params},{replacement}", True
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return value, False


def mask_messages(messages: List[dict]) -> Tuple[List[dict], bool]:
    """对 OpenAI 格式的 messages 做发送方向脱敏

    只动文本部分：多模态消息里的 image_url 等结构原样保留
    （图片脱敏已明确放弃，见决策 #9）。
    """
    if not messages:
        return messages, False

    changed = False
    result: List[dict] = []

    for message in messages:
        if not isinstance(message, dict):
            result.append(message)
            continue

        item = dict(message)
        content = item.get("content")

        if isinstance(content, str):
            item["content"], hit = mask_text(content)
            changed = changed or hit
        elif isinstance(content, list):
            parts = []
            for part in content:
                if not isinstance(part, dict):
                    parts.append(part)
                    continue

                new_part = dict(part)
                if isinstance(part.get("text"), str):
                    new_part["text"], hit = mask_text(part["text"])
                    changed = changed or hit

                holder = part.get("file")
                if isinstance(holder, dict):
                    new_holder = dict(holder)
                    for key in ("file_data", "data"):
                        value = holder.get(key)
                        if isinstance(value, str):
                            new_holder[key], hit = _mask_text_data_url(value)
                            changed = changed or hit
                            break
                    new_part["file"] = new_holder

                parts.append(new_part)
            item["content"] = parts

        result.append(item)

    return result, changed
