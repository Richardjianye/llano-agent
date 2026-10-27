"""回放集的打分口径（纯函数，不碰数据库、不碰网络）。

输入为测试会话的指标行，仅需要长度、工具名和耗时，不需要对话正文。

一行 = 一次上游请求（一轮）。字段：
  created_at        datetime 或 ISO 字符串
  response_chars    这一轮模型给同事看的文字长度（0 = 只有工具调用，用户什么都没看到）
  tool_skeleton     JSON 字符串或列表：[{"name": "terminal", "chars": 93}, ...]，是**整段对话到这一轮为止**
                    累计的工具调用骨架，所以取最后一行才是总数
  latency_ms / prompt_tokens / completion_tokens
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable, Optional

SILENT_GAP_SECONDS = 180


def _when(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" ", "T"))


def _chars(row: dict) -> int:
    return int(row.get("response_chars") or 0)


def _ordered(rows: Iterable[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (_when(r["created_at"]), int(r.get("id") or 0)))


def rounds_before_first_text(rows: Iterable[dict]) -> Optional[int]:
    """第一段文字出现之前，用户经历了几轮「只有工具调用」。没有出现过文字返回 None。"""
    for index, row in enumerate(_ordered(rows)):
        if _chars(row) > 0:
            return index
    return None


def seconds_before_first_text(rows: Iterable[dict]) -> Optional[float]:
    """从第一次请求到第一段文字出现的秒数（按各轮开始时间算，是下限）。"""
    ordered = _ordered(rows)
    for row in ordered:
        if _chars(row) > 0:
            return (_when(row["created_at"]) - _when(ordered[0]["created_at"])).total_seconds()
    return None


def tool_calls(rows: Iterable[dict]) -> list[str]:
    """整段对话里调用过的工具名（按顺序）。取最后一行的累计骨架。"""
    ordered = _ordered(rows)
    if not ordered:
        return []
    raw = ordered[-1].get("tool_skeleton")
    if not raw:
        return []
    try:
        skeleton = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    return [str(item.get("name", "?")) for item in skeleton if isinstance(item, dict)]


def silent_streaks(rows: Iterable[dict], gap_seconds: int = SILENT_GAP_SECONDS) -> list[dict]:
    """连续无文字回合段：相邻回合间隔 ≤ gap 且都没有文字的算同一段。"""
    streaks: list[dict] = []
    current: list[dict] = []
    for row in _ordered(rows):
        silent = _chars(row) == 0
        if silent and current and (_when(row["created_at"]) - _when(current[-1]["created_at"])).total_seconds() <= gap_seconds:
            current.append(row)
            continue
        if current:
            streaks.append(_streak(current))
        current = [row] if silent else []
    if current:
        streaks.append(_streak(current))
    return streaks


def _streak(rows: list[dict]) -> dict:
    return {
        "rounds": len(rows),
        "seconds": (_when(rows[-1]["created_at"]) - _when(rows[0]["created_at"])).total_seconds(),
    }


def narration_rate(rows: Iterable[dict], gap_seconds: int = 300) -> Optional[float]:
    """中间回合（后面还有下一轮）里带文字的比例。没有中间回合返回 None。"""
    ordered = _ordered(rows)
    mid = with_text = 0
    for row, following in zip(ordered, ordered[1:]):
        if (_when(following["created_at"]) - _when(row["created_at"])).total_seconds() > gap_seconds:
            continue
        mid += 1
        with_text += 1 if _chars(row) > 0 else 0
    return with_text / mid if mid else None


def summarize_session(rows: Iterable[dict]) -> dict:
    """一条回放（一个会话）的全部指标。"""
    ordered = _ordered(rows)
    tools = tool_calls(ordered)
    streaks = silent_streaks(ordered)
    return {
        "rounds": len(ordered),
        "rounds_before_first_text": rounds_before_first_text(ordered),
        "seconds_before_first_text": seconds_before_first_text(ordered),
        "tool_calls": len(tools),
        "terminal_calls": sum(1 for name in tools if name == "terminal"),
        "narration_rate": narration_rate(ordered),
        "longest_silent_rounds": max((s["rounds"] for s in streaks), default=0),
        "longest_silent_seconds": max((s["seconds"] for s in streaks), default=0.0),
        "model_seconds": sum(int(r.get("latency_ms") or 0) for r in ordered) / 1000,
        "prompt_tokens_last": int(ordered[-1].get("prompt_tokens") or 0) if ordered else 0,
    }
