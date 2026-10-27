"""流式转发的记账保障 —— 回归测试

## 这个文件为什么存在

端到端实测时发现：发了 4 次请求，用量表只记了 3 次，漏的是流式那次。

原因是记账代码写在生成器最后一个 `yield` 之后。客户端收到 `[DONE]` 就断开，
Starlette 随即关闭生成器，执行流走不到记账那几行 —— 这次调用的 token 白送。

真实场景里这比测试脚本更常见：用户点「停止生成」、关客户端、网络断开。
而且**越长的回答越容易被中途叫停**，漏掉的恰好是最贵的那些请求。
更糟的是它不报错、不留日志，只是账目悄悄少一笔。

所以这里钉死两条：

1. 客户端**提前断开**时，已产生的用量仍然入库
2. 正常读完时，用量当然也要入库（别为了修 1 把 2 弄坏）
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, AsyncGenerator, List, Optional, Tuple

import pytest


class _Caller:
    uid = "u-test"
    dept_id = 1
    role = "employee"


class _Model:
    id = 10
    model_name = "deepseek-v4-flash"
    # 单价按「元 / 百万 token」（决策 #25）。带上是为了验证流式记账
    # 同样会捎上单价快照 —— 漏了的话这一笔在报表里会被当成「无快照」，
    # 整行日聚合退回按当前单价回算，把当天正常的笔数一起拖下水
    billing_mode = "token"
    input_price_per_1m = Decimal("1")
    output_price_per_1m = Decimal("2")
    cached_input_price_per_1m = None
    price_per_call = None
    currency = "CNY"


class _Target:
    provider_id = 1


def _chunk(text: str, usage: Optional[dict] = None) -> dict:
    body: dict = {
        "choices": [{"delta": {"content": text}}],
    }
    if usage:
        body["usage"] = usage
    return body


async def _fake_stream(_target, _payload) -> AsyncGenerator[Tuple[str, Any], None]:
    """模拟真实上游 SSE：正文 + usage 尾 chunk + 标准 [DONE]"""
    yield 'data: {"x":1}', _chunk("你好")
    yield 'data: {"x":2}', _chunk("世界")
    yield 'data: {"x":3}', _chunk(
        "", {"prompt_tokens": 90, "completion_tokens": 68, "total_tokens": 158}
    )
    yield "data: [DONE]", None


@pytest.fixture
def chat_module(monkeypatch):
    """装好替身的 chat 模块

    记账改成同步收集到列表里，避免测试依赖 MySQL。
    """
    from router.v1 import chat

    recorded: List[Any] = []

    async def fake_record(facts, *args, **kwargs):
        recorded.append(facts)

    monkeypatch.setattr(chat, "record_usage", fake_record)
    monkeypatch.setattr(chat, "stream_upstream", _fake_stream)

    return chat, recorded


async def _drain(gen, stop_at_done: bool) -> List[str]:
    """消费生成器。stop_at_done=True 模拟客户端收到 [DONE] 就断开"""
    out: List[str] = []
    async for piece in gen:
        out.append(piece)
        if stop_at_done and "[DONE]" in piece:
            break
    # Starlette 在客户端断开后会关闭生成器，这一步是复现的关键
    await gen.aclose()
    return out


async def _settle(chat) -> None:
    """等后台记账任务跑完

    记账是 create_task 丢到事件循环上的（不能在关闭中的生成器里 await），
    所以断言之前必须给它一次调度机会。
    """
    import asyncio

    pending = set(chat._PENDING_RECORDS)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.sleep(0)


def _run(gen_factory, stop_at_done: bool, chat):
    import asyncio

    async def main():
        await _drain(gen_factory(), stop_at_done)
        await _settle(chat)

    asyncio.run(main())


def _make(chat):
    import time

    from services.audit_envelope import AuditEnvelope

    return chat._stream_and_record(
        _Caller(), "rid-1", _Model(), _Target(),
        payload={"stream": True},
        messages=[{"role": "user", "content": "你好"}],
        audit=AuditEnvelope(
            messages=[{"role": "user", "content": "你好"}],
            session_id="20260821_151706_ac236f",
        ),
        masked=False,
        started=time.perf_counter(),
    )


# ==================== 断言 ====================


def test_客户端提前断开仍然记账(chat_module):
    """这是漏洞本体：客户端收到 [DONE] 立刻断开，账不能丢"""
    chat, recorded = chat_module

    _run(lambda: _make(chat), stop_at_done=True, chat=chat)

    assert len(recorded) == 1, "客户端断开后用量没入库 —— 这次调用的 token 白送了"
    assert recorded[0].stream is True
    assert recorded[0].total_tokens > 0


def test_正常读完也记账(chat_module):
    """别为了修上一条把正常路径弄坏"""
    chat, recorded = chat_module

    _run(lambda: _make(chat), stop_at_done=False, chat=chat)

    assert len(recorded) == 1
    facts = recorded[0]
    # 上游给了 usage，就必须用上游的数，不能退化成估算
    assert facts.prompt_tokens == 90
    assert facts.completion_tokens == 68
    assert facts.tokens_estimated is False
    assert facts.status == "success"
    assert facts.aborted is False


def test_标准上游结束帧只向客户端发送一次(chat_module):
    """重复 [DONE] 会让正常关闭落进中断分支，污染 aborted 统计"""
    chat, recorded = chat_module

    async def main():
        output = await _drain(_make(chat), stop_at_done=False)
        await _settle(chat)
        return output

    import asyncio

    output = asyncio.run(main())
    assert sum("[DONE]" in piece for piece in output) == 1
    assert recorded[0].aborted is False


def test_只记一次不重复(chat_module):
    """finally 会在多种路径上触发，别把一次调用记成两笔"""
    chat, recorded = chat_module

    _run(lambda: _make(chat), stop_at_done=False, chat=chat)

    assert len(recorded) == 1


def test_断开时回复内容仍被保留(chat_module):
    """审计要能看到「模型已经说出去的那部分」，哪怕用户没看完"""
    chat, recorded = chat_module

    _run(lambda: _make(chat), stop_at_done=True, chat=chat)

    assert "你好" in recorded[0].response_text


def test_流式记账捎上单价快照(chat_module):
    """漏了快照不会报错，只会让这一笔被当成「无快照」——
    整行日聚合于是退回按当前单价回算，把当天正常的笔数一起拖下水"""
    from services.usage import build_price_snapshot

    chat, recorded = chat_module

    _run(lambda: _make(chat), stop_at_done=True, chat=chat)

    facts = recorded[0]
    assert facts.price is not None and facts.price.configured
    # 90 输入 × 1元/M + 68 输出 × 2元/M
    assert build_price_snapshot(facts).cost == Decimal("0.000226")
