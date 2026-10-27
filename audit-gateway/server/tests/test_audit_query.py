"""会话审计读取侧的纯逻辑测试

重点覆盖三件「错了不报错、只是悄悄给出误导结论」的事：

1. **摘要不能变相下发全文**：列表页摘要一旦超长，
   「列表 + 详情抽屉」方案的审计颗粒度就白设计了（决策 #8）。
2. **脏数据不能让详情页 500**：被截断的半截 JSON 是必然会出现的，
   打不开的详情页等于这条记录不存在。
3. **关键词的通配符必须转义**：搜「100%」变成匹配任意内容，
   审计员会以为全公司都在聊这个。
"""

from __future__ import annotations

import json

import pytest
from services.audit_query import (CONVERSATION_STATUSES, MAX_PAGE_DEPTH,
                                  MAX_PAGE_SIZE, MIN_KEYWORD_LEN,
                                  PREVIEW_MAX_CHARS, AuditQueryError,
                                  access_logs_to_csv, extract_markers,
                                  format_size, make_preview,
                                  normalize_choice, normalize_keyword,
                                  normalize_page, normalize_page_size,
                                  parse_tool_skeleton, summarize_tools,
                                  to_csv, turn_tool_delta)

MARKER = "[[attachment:a604bee69a51 图.png image/png 3008B]]"


def test_短码去重且保序():
    text = f"a {MARKER} b {MARKER} c [[attachment:ffffffffffff x image/png 1B]]"
    assert extract_markers(text) == ["a604bee69a51", "ffffffffffff"]


def test_没有附件时短码为空():
    assert extract_markers("普通的一段话") == []
    assert extract_markers("") == []


# ==================== 参数校验 ====================


def test_关键词转义通配符():
    """搜「100%」不该变成匹配任意内容"""
    assert normalize_keyword("100%") == "100\\%"
    assert normalize_keyword("a_b") == "a\\_b"
    assert normalize_keyword("c\\d") == "c\\\\d"


def test_关键词去空白():
    assert normalize_keyword("  合同  ") == "合同"


def test_空关键词视为不筛选():
    assert normalize_keyword(None) is None
    assert normalize_keyword("   ") is None


def test_关键词过短被拒():
    """单字符会退化成全表 LIKE '%a%'，在几百万行上就是一次事故"""
    with pytest.raises(AuditQueryError):
        normalize_keyword("a" * (MIN_KEYWORD_LEN - 1))


def test_页大小上限():
    assert normalize_page_size(MAX_PAGE_SIZE) == MAX_PAGE_SIZE
    with pytest.raises(AuditQueryError):
        normalize_page_size(MAX_PAGE_SIZE + 1)
    with pytest.raises(AuditQueryError):
        normalize_page_size(0)


def test_翻页深度上限():
    """深翻页的 OFFSET 是数据库逐行数过去的，第 500 页比第 1 页慢两个数量级。
    真要翻到那儿，该做的是缩小时间范围，不是一直点下一页"""
    assert normalize_page(1, 20) == 1
    assert normalize_page(MAX_PAGE_DEPTH // 20, 20) == MAX_PAGE_DEPTH // 20

    with pytest.raises(AuditQueryError):
        normalize_page(MAX_PAGE_DEPTH // 20 + 1, 20)
    with pytest.raises(AuditQueryError):
        normalize_page(0, 20)


# ==================== 枚举型筛选值 ====================


def test_写错的状态要报错而不是查出0条():
    """放行的话，`status=succes` 会安静地查出 0 条，
    审计员读到的是「这个人没聊过」—— 一个完全错误却很有说服力的结论"""
    with pytest.raises(AuditQueryError):
        normalize_choice("succes", CONVERSATION_STATUSES, "状态")


def test_合法状态原样通过():
    assert normalize_choice("success", CONVERSATION_STATUSES, "状态") == "success"


def test_空值视为不筛选():
    assert normalize_choice(None, CONVERSATION_STATUSES, "状态") is None
    assert normalize_choice("  ", CONVERSATION_STATUSES, "状态") is None


def test_中断不是状态取值():
    """中断与 status 正交，是单独一列 aborted（决策 #28）"""
    with pytest.raises(AuditQueryError):
        normalize_choice("aborted", CONVERSATION_STATUSES, "状态")


# ==================== 展示 ====================


def test_体积格式化():
    # 远程附件没有字节数，显示「未知」而不是 0 B —— 后者会被读成「空文件」
    assert format_size(None) == "未知"
    assert format_size(0) == "未知"
    assert format_size(900) == "900 B"
    assert format_size(3008) == "2.9 KB"
    assert format_size(5 * 1024 * 1024) == "5.0 MB"


# ==================== 导出 ====================


def test_导出只含摘要不含全文():
    """全文导出等于把会话库打包带走，一旦落到本地磁盘就脱离留存期限管辖"""
    rows = [{
        "created_at": "2026-08-08 10:00:00",
        "username": "admin",
        "dept_name": "管理部门",
        "role": "president",
        "model_name": "deepseek-v4-flash",
        "status": "success",
        "attachment_count": 2,
        "truncated": True,
        "masked": True,
        "preview": "看看这张图",
        "response_preview": "好的",
    }]
    csv_text = to_csv(rows, note="口径说明")

    assert "admin" in csv_text and "管理部门" in csv_text
    assert "看看这张图" in csv_text
    assert "口径说明" in csv_text
    # 布尔值要落成中文，导出的表格是给人看的
    assert ",是,是," in csv_text


def test_导出空结果仍有表头():
    """空表格比「什么都没下载下来」更容易解释"""
    csv_text = to_csv([], note="口径说明")

    assert csv_text.startswith("时间,用户,部门")
    assert "口径说明" in csv_text


@pytest.mark.parametrize("payload", ["=1+1", "+cmd", "-2+3", "@SUM(A1:A2)"])
def test_导出会转义表格公式(payload):
    rows = [{"username": payload, "preview": payload}]
    csv_text = to_csv(rows)
    assert f"'{payload}" in csv_text


def test_查阅日志导出不含会话内容():
    """这份是拿去交材料的：只有「谁看了谁的什么」，一个字的聊天内容都没有，
    所以可以放心落到本地磁盘 —— 会话导出只出摘要正是因为没有这个前提"""
    rows = [{
        "created_at": "2026-08-08 10:00:00",
        "actor_name": "admin",
        "actor_role": "president",
        "action": "detail",
        "action_label": "查看全文",
        "target_name": "阿蒙",
        "target_conversation_id": 42,
        "result_count": 1,
        "client_ip": "10.0.0.2",
        "query_json": None,
    }]
    csv_text = access_logs_to_csv(rows, note="口径说明")

    assert csv_text.startswith("时间,查阅人")
    assert "查看全文" in csv_text and "阿蒙" in csv_text
    assert "口径说明" in csv_text


def test_查阅日志导出空结果仍有表头():
    assert access_logs_to_csv([]).startswith("时间,查阅人")


# ==================== 工具骨架汇总（决策 #52 · C1） ====================


def test_工具汇总按字数降序_不是按次数():
    """审计关心的是「哪个工具把最多公司数据送出去了」。

    按次数排的话，调了 40 次每次 20 字的 terminal 会把调了 3 次每次
    18000 字的 read_file 压到看不见的地方 —— 那正好把最该看的藏了。
    """
    entries = [{"name": "terminal", "chars": 20} for _ in range(40)]
    entries += [{"name": "read_file", "chars": 18000} for _ in range(3)]

    summary = summarize_tools(entries)

    assert [item["name"] for item in summary] == ["read_file", "terminal"]
    assert summary[0] == {"name": "read_file", "count": 3, "chars": 54000}
    assert summary[1] == {"name": "terminal", "count": 40, "chars": 800}


def test_字数相同时按名字排_结果要稳定():
    """否则同一条记录刷新两次 chip 顺序可能不一样，看的人会以为数据变了。"""
    summary = summarize_tools(
        [{"name": "zebra", "chars": 10}, {"name": "alpha", "chars": 10}]
    )

    assert [item["name"] for item in summary] == ["alpha", "zebra"]


def test_工具汇总把63次压成几行():
    """2026-08-23 实测的形状：一次 agent 会话 63 次工具调用，全摊开是一面墙。"""
    entries = (
        [{"name": "terminal", "chars": 500}] * 30
        + [{"name": "read_file", "chars": 12000}] * 28
        + [{"name": "search_files", "chars": 800}] * 5
    )

    summary = summarize_tools(entries)

    assert len(summary) == 3
    assert sum(item["count"] for item in summary) == 63


def test_没有工具时汇总是空的_不炸():
    assert summarize_tools([]) == []


# ==================== 这一轮新增的工具调用 ====================


def _tools(*names) -> list:
    return [{"name": n, "chars": 10} for n in names]


def test_只算这一轮新增的_不把历史累计当成本轮():
    """Hermes 每轮重发完整历史，骨架里躺着前面所有轮次的工具结果。

    直接展示的话，58 轮里每一行都写着 `terminal × 43`，读起来像每轮都调了
    43 次 —— **数字对得上，含义是错的**，是那种看起来很正常、没人会质疑的谎。
    """
    previous = _tools("terminal", "terminal", "read_file")
    current = _tools("terminal", "terminal", "read_file", "read_file", "terminal")

    delta = turn_tool_delta(current, previous)

    assert summarize_tools(delta) == [
        {"name": "read_file", "count": 1, "chars": 10},
        {"name": "terminal", "count": 1, "chars": 10},
    ]


def test_会话第一轮算不出上一轮_返回None而不是空列表():
    """None 是「算不出」，[] 是「这一轮没调工具」——两者界面上该长得不一样。"""
    assert turn_tool_delta(_tools("terminal"), None) is None


def test_这一轮确实没调工具时是空列表_不是None():
    same = _tools("terminal", "read_file")

    assert turn_tool_delta(same, list(same)) == []


def test_上下文被压缩重写过历史时对不齐_返回None():
    """压缩会把历史改写掉，前缀关系不再成立。

    此时宁可说「算不出」也不能把整包累计当成本轮新增 —— 那会在界面上
    突然冒出一轮「调用了 69 次工具」。
    """
    previous = _tools("terminal", "terminal", "read_file")
    compressed = _tools("summary", "read_file")

    assert turn_tool_delta(compressed, previous) is None


def test_上一轮比这一轮还长时也算对不齐():
    assert turn_tool_delta(_tools("a"), _tools("a", "b", "c")) is None


def test_长度够但内容对不上时也算对不齐():
    """⚠️ 这条是变异验证补出来的。

    原来只有「压缩后变短」和「上一轮更长」两条用例，两条都会被长度判断拦下 ——
    于是前缀校验拆掉也不报红，说明它根本没被测到。这里让长度**只增不减**、
    但历史被改写过，逼着前缀校验单独站出来干活。
    """
    previous = _tools("terminal", "terminal", "read_file")
    rewritten = _tools("summary", "terminal", "read_file", "terminal")

    assert turn_tool_delta(rewritten, previous) is None
