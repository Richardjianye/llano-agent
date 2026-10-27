"""Chat Completions ⇄ Codex Responses 协议翻译（提取自 codex2api proxy/translator.go）

两件事：

- ``build_responses_request``：把 OpenAI chat/completions 请求翻成 Codex
  ``/responses`` 请求体（Codex 安全子集：采样参数一律不发，上游会 400）。
- ``StreamTranslator``：把 ``/responses`` 的 SSE 事件翻回 chat.completion.chunk，
  同时攒出非流式装配所需的正文 / 工具调用 / usage。

事件映射与 codex2api StreamTranslator 保持同一口径：
text.delta → content 增量；reasoning_summary_text.delta → reasoning 增量；
output_item.added(function_call) → 工具调用公告块；function_call_arguments.delta
→ 参数增量块；completed/incomplete → 终结块（带 usage）；failed → error 块。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

MAX_TOOLS = 128

# 工具结果里的图片被挪走后留下的占位文案（与 codex2api 同文案，保证无图请求
# 的 prompt 前缀不变）
TOOL_RESULT_IMAGE_MOVED_MARKER = "[Tool output image moved to the following user message]"
TOOL_RESULT_IMAGE_ATTRIBUTION = "[Tool output image for call %s]"


class TranslateError(RuntimeError):
    """请求侧无法翻译（结构性错误，直接 400 退回客户端）"""


# ==================== 请求侧：chat → responses ====================


def new_chunk_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def build_responses_request(payload: Dict[str, Any], cache_key: str = "") -> Dict[str, Any]:
    """把 chat/completions 请求体翻成 Codex /responses 请求体

    只保留 Codex 后端支持的字段（参照 codex2api buildChatResponsesRequest）：
    temperature / top_p / stop 等采样参数**不发**（Codex 后端不接受）；
    store=False、include=reasoning.encrypted_content 与真实 Codex CLI 同款。
    """
    model = payload.get("model")
    if not model:
        raise TranslateError("缺少 model 参数")

    messages = payload.get("messages") or []
    out: Dict[str, Any] = {
        "model": model,
        "stream": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "instructions": "",
        "input": messages_to_input(sanitize_tool_history(messages)),
    }

    effort = normalize_reasoning_effort(
        payload.get("reasoning_effort") or "", str(model)
    )
    reasoning: Dict[str, Any] = {"summary": "auto"}
    if effort:
        reasoning["effort"] = effort
    out["reasoning"] = reasoning

    tools = payload.get("tools") or []
    if isinstance(tools, list) and tools:
        converted = convert_tools(tools)
        if converted:
            out["tools"] = converted

    tool_choice = payload.get("tool_choice")
    if tool_choice is not None:
        out["tool_choice"] = convert_tool_choice(tool_choice)

    response_format = payload.get("response_format")
    if isinstance(response_format, dict):
        fmt = convert_response_format(response_format)
        if fmt is not None:
            out["text"] = {"format": fmt}

    if cache_key:
        out["prompt_cache_key"] = cache_key

    return out


def normalize_reasoning_effort(effort: str, model: str = "") -> str:
    """档位钳制，与 codex2api normalizeReasoningEffortForModel 同口径

    max 只有 gpt-5.6+ 接受，旧模型发 max 上游直接报 Invalid value。
    """
    effort = (effort or "").strip().lower()
    if not effort:
        return ""
    if effort == "max":
        return "max" if model_supports_max_effort(model) else "xhigh"
    if effort in ("none", "minimal", "low", "medium", "high", "xhigh", "ultra"):
        return effort
    return "high"


def model_supports_max_effort(model: str) -> bool:
    model = (model or "").strip().lower()
    if not model.startswith("gpt-"):
        return False
    if model.startswith("gpt-daybreak-"):
        return True
    version = model[len("gpt-"):]
    if "-" in version:
        version = version.split("-", 1)[0]
    parts = version.split(".")
    try:
        major = int(parts[0])
    except ValueError:
        return False
    if major > 5:
        return True
    if len(parts) < 2:
        return False
    try:
        minor = int(parts[1])
    except ValueError:
        return False
    return major == 5 and minor >= 6


def _content_parts(role: str, content: Any) -> List[Dict[str, Any]]:
    """消息 content（字符串或 parts 数组）→ Responses content parts"""
    text_type = "output_text" if role == "assistant" else "input_text"
    parts: List[Dict[str, Any]] = []
    if content is None:
        return parts
    if isinstance(content, str):
        if content:
            parts.append({"type": text_type, "text": content})
        return parts
    if not isinstance(content, list):
        return parts
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text" and item.get("text") is not None:
            parts.append({"type": text_type, "text": item.get("text") or ""})
        elif kind == "image_url":
            url = (item.get("image_url") or {}).get("url") if isinstance(item.get("image_url"), dict) else None
            if url:
                parts.append({"type": "input_image", "image_url": url})
        elif kind == "file":
            file_info = item.get("file") or {}
            if isinstance(file_info, dict) and (file_info.get("file_id") or file_info.get("file_data")):
                part: Dict[str, Any] = {"type": "input_file"}
                if file_info.get("filename"):
                    part["filename"] = file_info["filename"]
                if file_info.get("file_data"):
                    part["file_data"] = file_info["file_data"]
                if file_info.get("file_id"):
                    part["file_id"] = file_info["file_id"]
                parts.append(part)
    return parts


def _tool_output_and_images(content: Any) -> Tuple[str, List[str]]:
    """tool 消息 → (function_call_output 文本, 抽出的图片 URL 列表)"""
    if not isinstance(content, list):
        return _content_to_text(content), []
    images = [
        (item.get("image_url") or {}).get("url")
        for item in content
        if isinstance(item, dict) and item.get("type") == "image_url"
        and isinstance(item.get("image_url"), dict) and (item.get("image_url") or {}).get("url")
    ]
    if not images:
        return _content_to_text(content), []
    texts = [
        item.get("text") or ""
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    output = "\n".join(t for t in texts if t) or TOOL_RESULT_IMAGE_MOVED_MARKER
    return output, images


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def sanitize_tool_history(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """清掉参数非法 JSON 的历史工具调用及其结果（与 codex2api 同策略）

    空 arguments 是「空对象」的常见简写，归一成 {}；参数非法的调用连同它的
    tool 结果一起删——上游会因为孤儿 function_call_output 直接拒绝整个请求。
    """
    if not messages:
        return messages

    invalid_ids = set()
    valid_ids = set()
    filtered: List[Dict[str, Any]] = []
    empty_calls = 0

    for msg in messages:
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            filtered.append(msg)
            continue
        calls = []
        for call in msg["tool_calls"]:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") or {}
            args = (fn.get("arguments") or "").strip()
            if not args:
                fn = dict(fn)
                fn["arguments"] = "{}"
                call = dict(call)
                call["function"] = fn
                args = "{}"
            try:
                json.loads(args)
                valid = True
            except ValueError:
                valid = False
            call_id = (call.get("id") or "").strip()
            if valid:
                if call_id:
                    valid_ids.add(call_id)
                calls.append(call)
            elif call_id:
                invalid_ids.add(call_id)
            else:
                empty_calls += 1
        invalid_ids -= valid_ids
        new_msg = dict(msg)
        new_msg["tool_calls"] = calls
        if not calls and not _content_to_text(msg.get("content")).strip():
            continue
        filtered.append(new_msg)

    if invalid_ids or empty_calls:
        kept = []
        for msg in filtered:
            if msg.get("role") == "tool":
                call_id = (msg.get("tool_call_id") or "").strip()
                if not call_id and empty_calls > 0:
                    empty_calls -= 1
                    continue
                if call_id in invalid_ids:
                    continue
            kept.append(msg)
        filtered = kept

    return filtered


def messages_to_input(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """chat messages → Responses input items（角色/工具调用语义对齐 codex2api）"""
    input_items: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role == "tool":
            output, images = _tool_output_and_images(msg.get("content"))
            input_items.append({
                "type": "function_call_output",
                "call_id": msg.get("tool_call_id") or "",
                "output": output,
            })
            if images:
                media_parts: List[Dict[str, Any]] = [{
                    "type": "input_text",
                    "text": TOOL_RESULT_IMAGE_ATTRIBUTION % (msg.get("tool_call_id") or ""),
                }]
                media_parts += [{"type": "input_image", "image_url": u} for u in images]
                input_items.append({"type": "message", "role": "user", "content": media_parts})
        elif role == "assistant":
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                text = _content_to_text(msg.get("content"))
                if text:
                    input_items.append({
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    })
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function") or {}
                    input_items.append({
                        "type": "function_call",
                        "call_id": call.get("id") or "",
                        "name": fn.get("name") or "",
                        "arguments": fn.get("arguments") or "{}",
                    })
            else:
                input_items.append({
                    "type": "message",
                    "role": "assistant",
                    "content": _content_parts("assistant", msg.get("content")),
                })
        elif role == "system":
            input_items.append({
                "type": "message",
                "role": "developer",
                "content": _content_parts("system", msg.get("content")),
            })
        else:
            input_items.append({
                "type": "message",
                "role": "user",
                "content": _content_parts("user", msg.get("content")),
            })
    return input_items


def convert_tools(raw_tools: List[Any]) -> List[Dict[str, Any]]:
    """OpenAI 工具格式 → Codex 格式：function 包装展开为顶层字段"""
    out: List[Dict[str, Any]] = []
    for tool in raw_tools[:MAX_TOOLS]:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "function":
            continue
        fn = tool.get("function") or {}
        if not fn.get("name"):
            continue
        item: Dict[str, Any] = {
            "type": "function",
            "name": fn["name"],
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        }
        if fn.get("description"):
            item["description"] = fn["description"]
        out.append(item)
    return out


def convert_tool_choice(tool_choice: Any) -> Any:
    if isinstance(tool_choice, str):
        value = tool_choice.strip()
        if value in ("auto", "none", "required"):
            return value
        raise TranslateError(f"tool_choice {tool_choice!r} 无法映射到 Responses")
    if isinstance(tool_choice, dict):
        kind = (tool_choice.get("type") or "").strip()
        if kind == "function":
            fn = tool_choice.get("function") or {}
            name = (tool_choice.get("name") or fn.get("name") or "").strip()
            if not name:
                raise TranslateError("function 类型的 tool_choice 缺少 name")
            return {"type": "function", "name": name}
        if kind == "custom":
            name = (tool_choice.get("name") or "").strip()
            if not name:
                raise TranslateError("custom 类型的 tool_choice 缺少 name")
            return {"type": "custom", "name": name}
        raise TranslateError(f"tool_choice type {kind!r} 无法映射到 Responses")
    raise TranslateError("tool_choice 格式无法映射到 Responses")


def convert_response_format(response_format: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """chat response_format → Responses text.format"""
    kind = response_format.get("type")
    if kind == "json_object":
        return {"type": "json_object"}
    if kind == "json_schema":
        spec = response_format.get("json_schema") or {}
        if not isinstance(spec, dict) or not spec.get("schema"):
            return None
        fmt: Dict[str, Any] = {
            "type": "json_schema",
            "name": spec.get("name") or "response",
            "schema": spec["schema"],
        }
        if spec.get("strict"):
            fmt["strict"] = True
        return fmt
    return None


# ==================== 响应侧：responses SSE → chat chunks ====================


def usage_to_chat(usage: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Responses usage → chat usage（口径对齐审计网关 extract_usage/extract_breakdown）"""
    if not isinstance(usage, dict):
        return None
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    reasoning_tokens = int((usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0)
    cached_tokens = int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)

    chat_usage: Dict[str, Any] = {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    if cached_tokens:
        chat_usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    if reasoning_tokens:
        chat_usage["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
    return chat_usage


def _incomplete_finish_reason(reason: str) -> str:
    if reason == "max_output_tokens":
        return "length"
    if reason == "content_filter":
        return "content_filter"
    return "stop"


class StreamTranslator:
    """有状态流式翻译器：Responses 事件 → chat.completion.chunk 字典

    与无状态版的区别是追踪 function_call 的索引映射（OpenAI 流式协议里
    同一次工具调用的所有增量共享一个自增 index），并为非流式装配攒状态。
    """

    def __init__(self, model: str, chunk_id: Optional[str] = None, created: Optional[int] = None):
        self.model = model
        self.chunk_id = chunk_id or new_chunk_id()
        self.created = created or int(__import__("time").time())
        self.text_parts: List[str] = []
        self.reasoning_parts: List[str] = []
        self.tool_calls: Dict[int, Dict[str, str]] = {}
        self._index_by_key: Dict[str, int] = {}
        self._index_by_output: Dict[int, int] = {}
        self._next_index = 0
        self.has_tool_calls = False
        self.usage: Optional[Dict[str, Any]] = None
        self.service_tier = ""
        self.failed_message = ""

    # ---- chunk 构造 ----

    def _chunk(self, delta: Dict[str, Any], finish_reason: Optional[str] = None,
               usage: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        choice: Dict[str, Any] = {"index": 0, "delta": delta}
        if finish_reason is not None:
            choice["finish_reason"] = finish_reason
        chunk: Dict[str, Any] = {
            "id": self.chunk_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [choice],
        }
        if usage is not None:
            chunk["usage"] = usage
        if self.service_tier:
            chunk["service_tier"] = self.service_tier
        return chunk

    def _error_chunk(self, message: str, details: Any = None) -> Dict[str, Any]:
        err: Dict[str, Any] = {"message": message, "type": "upstream_error"}
        if details is not None:
            err["details"] = details
        return {"error": err}

    # ---- 工具调用索引 ----

    def _tool_index(self, event: Dict[str, Any]) -> Optional[int]:
        for key in (event.get("item_id"), event.get("call_id"), event.get("item", {}).get("id") if isinstance(event.get("item"), dict) else None):
            if key and str(key) in self._index_by_key:
                return self._index_by_key[str(key)]
        output_index = event.get("output_index")
        if isinstance(output_index, int) and output_index in self._index_by_output:
            return self._index_by_output[output_index]
        return None

    # ---- 主入口 ----

    def translate(self, event: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], bool, bool]:
        """翻译一个 Responses 事件。返回 (chunk|None, 是否终结, 是否失败)"""
        etype = str(event.get("type") or "")

        if etype == "response.output_text.delta":
            delta = str(event.get("delta") or "")
            if delta:
                self.text_parts.append(delta)
            return self._chunk({"content": delta}), False, False

        if etype in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            delta = str(event.get("delta") or "")
            if delta:
                self.reasoning_parts.append(delta)
            return self._chunk({"reasoning": delta, "reasoning_content": delta}), False, False

        if etype == "response.output_item.added":
            item = event.get("item") or {}
            item_type = item.get("type")
            if item_type not in ("function_call", "custom_tool_call"):
                return None, False, False
            call_id = str(item.get("call_id") or item.get("id") or "")
            name = str(item.get("name") or "")
            item_id = str(item.get("id") or call_id)
            idx = self._next_index
            self._next_index += 1
            for key in (item_id, call_id):
                if key:
                    self._index_by_key[key] = idx
            output_index = event.get("output_index")
            if isinstance(output_index, int):
                self._index_by_output[output_index] = idx
            self.has_tool_calls = True
            self.tool_calls[idx] = {"id": call_id, "name": name, "arguments": "", "type": item_type}
            return self._chunk({
                "role": "assistant",
                "tool_calls": [{
                    "index": idx,
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": ""},
                }],
            }), False, False

        if etype == "response.function_call_arguments.delta":
            idx = self._tool_index(event)
            if idx is None:
                return None, False, False
            delta = str(event.get("delta") or "")
            self.tool_calls[idx]["arguments"] += delta
            return self._chunk({
                "tool_calls": [{"index": idx, "function": {"arguments": delta}}]
            }), False, False

        if etype == "response.function_call_arguments.done":
            idx = self._tool_index(event)
            if idx is None:
                return None, False, False
            args = str(event.get("arguments") or "")
            self.tool_calls[idx]["arguments"] = args
            return None, False, False

        if etype == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") in ("function_call", "custom_tool_call"):
                idx = self._tool_index(event)
                if idx is not None and item.get("arguments") is not None:
                    self.tool_calls[idx]["arguments"] = str(item.get("arguments") or "")
            return None, False, False

        if etype in ("response.completed", "response.incomplete"):
            response = event.get("response") or {}
            self.usage = usage_to_chat(response.get("usage"))
            self.service_tier = str(response.get("service_tier") or "")
            finish = "tool_calls" if self.has_tool_calls else "stop"
            if etype == "response.incomplete":
                reason = str((response.get("incomplete_details") or {}).get("reason") or "")
                finish = _incomplete_finish_reason(reason)
            # 参数非法 JSON 的工具调用整个回合都是废的，明确报错而不是装正常收尾
            bad = [t for t in self.tool_calls.values() if not _valid_json_args(t["arguments"])]
            if bad and etype == "response.completed":
                self.failed_message = (
                    f"上游工具调用 {bad[0]['name'] or 'unknown'} 的参数不是合法 JSON"
                )
                return self._error_chunk(self.failed_message), True, True
            return self._chunk({}, finish, self.usage), True, False

        if etype == "response.failed":
            response = event.get("response") or {}
            err = response.get("error") or {}
            message = str(err.get("message") or "Codex upstream error")
            self.failed_message = message
            return self._error_chunk(message, err.get("details")), True, True

        # 其余事件：带字符串 delta 的兜底为正文增量，否则忽略
        delta = event.get("delta")
        if isinstance(delta, str) and delta:
            self.text_parts.append(delta)
            return self._chunk({"content": delta}), False, False
        return None, False, False

    # ---- 非流式装配 ----

    def final_response(self) -> Dict[str, Any]:
        """把翻译过程攒下的状态装配成 chat.completion 响应体"""
        if self.failed_message:
            return self._error_chunk(self.failed_message)
        message: Dict[str, Any] = {
            "role": "assistant",
            "content": "".join(self.text_parts) or None,
        }
        finish = "stop"
        if self.has_tool_calls:
            finish = "tool_calls"
            message["tool_calls"] = [
                {
                    "id": t["id"],
                    "type": "function",
                    "function": {"name": t["name"], "arguments": t["arguments"] or "{}"},
                }
                for _, t in sorted(self.tool_calls.items())
            ]
        elif message["content"] is None:
            message["content"] = ""
        response: Dict[str, Any] = {
            "id": self.chunk_id,
            "object": "chat.completion",
            "created": self.created,
            "model": self.model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": finish,
            }],
            "usage": self.usage or {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }
        if self.service_tier:
            response["service_tier"] = self.service_tier
        return response


def _valid_json_args(args: str) -> bool:
    if not (args or "").strip():
        return True
    try:
        json.loads(args)
        return True
    except ValueError:
        return False
