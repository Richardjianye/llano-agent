"""审计正文写入侧整形的回归（决策 #52 的内容模型）

钉住的核心不变量：**用户真正问的那句话必须完整活下来**，而系统提示词、
本地工具链只留指纹与骨架。

上一版这里钉的是另一条 —— 「落库的字符串永远还是一个能解析的 JSON 数组」。
那条不变量随 `request_messages` 一起退役了：用户输入现在是独立一列纯文本，
没有结构可切。**但 `tool_skeleton` 仍然是 JSON**，真要给它加限制时记得
限条数、不要切字符串（当年那个 bug 的完整形状见 services/audit_content.py 模块注释）。
"""

import json
import re

import pytest

from services.audit_content import (CONTENT_MAX_CHARS, TRUNCATION_SUFFIX,
                                    elide_middle,
                                    last_user_text, make_user_preview,
                                    shape_audit_content, system_prompt_text,
                                    tool_skeleton, truncate_text)
from services.audit_query import PREVIEW_MAX_CHARS, parse_tool_skeleton


def hermes_like(system_chars: int, question: str = "帮我看下这个订单为什么没发货") -> list:
    """一次 Hermes agent 调用的形状：巨长的系统提示在前，用户那句话在最后。"""
    return [
        {"role": "system", "content": "You are Hermes Agent. " + "指令细则。" * system_chars},
        {"role": "user", "content": "上一轮的问题"},
        {"role": "assistant", "content": "上一轮的回答"},
        {"role": "user", "content": question},
    ]


def test_last_user_text_取最后一条而不是第一条():
    messages = [
        {"role": "user", "content": "第一个问题"},
        {"role": "assistant", "content": "回答"},
        {"role": "user", "content": "第二个问题"},
    ]

    assert last_user_text(messages) == "第二个问题"


def test_last_user_text_不把工具结果当成用户提问():
    messages = [
        {"role": "user", "content": "查一下库存"},
        {"role": "tool", "content": "{\"stock\": 12}"},
    ]

    assert last_user_text(messages) == "查一下库存"


def test_回复正文是纯文本_直接切就行():
    long_text = "回答" * 20000

    shaped, truncated = truncate_text(long_text)

    assert truncated is True
    assert len(shaped) <= CONTENT_MAX_CHARS + 10
    assert shaped.endswith("…（已截断）")


def test_回复正文没超长时不动它():
    assert truncate_text("短回答") == ("短回答", False)


# ==================== elide_middle 本身 ====================


def test_elide_middle_绝不超限():
    """省略标记的长度取决于省略字数，省略字数又取决于标记长度 —— 互相依赖。

    位数跨越（99→100→1000）时最容易多出一两个字，逐个额度扫一遍钉死。
    """
    text = "内容" * 5000

    for limit in list(range(80, 400)) + [1000, 4000, 9999]:
        assert len(elide_middle(text, limit)) <= limit, f"limit={limit} 超了"


def test_elide_middle_标出的省略字数是准的():
    text = "a" * 1000
    result = elide_middle(text, 200)

    omitted = int(re.search(r"中间省略 (\d+) 字", result).group(1))
    kept = len(result) - len(f"…（中间省略 {omitted} 字）…")

    assert kept + omitted == len(text)


def test_elide_middle_额度太小时退回截头():
    """头尾各剩十几个字加一个省略标记，比单纯截头更难读。"""
    result = elide_middle("内容" * 500, 40)

    assert result.endswith(TRUNCATION_SUFFIX)
    assert "中间省略" not in result
    assert len(result) <= 40


def test_elide_middle_没超长时原样返回():
    assert elide_middle("短内容", 100) == "短内容"


# ==================== 列表页摘要 ====================


def test_列表摘要显示的是问题而不是粘贴的文档开头():
    """症状：一页 20 条全都长成 `> ## Documentation Index`，看不出谁在问什么。"""
    question = "这个工作流为什么跑不出视频？"
    text = "> ## Documentation Index\n" + "节点说明。" * 2000 + "\n---\n" + question

    preview = make_user_preview(text, PREVIEW_MAX_CHARS)

    assert preview.endswith(question)
    assert preview.startswith("> ## Documentation Index")
    assert len(preview) <= PREVIEW_MAX_CHARS


def test_列表摘要把换行压成空格():
    """列表是单行显示的，带换行会让 CSV 导出跟着错行。"""
    assert "\n" not in make_user_preview("第一行\n第二行\n第三行", PREVIEW_MAX_CHARS)


def test_列表摘要短内容原样给出():
    assert make_user_preview("运费怎么算", PREVIEW_MAX_CHARS) == "运费怎么算"


def test_列表摘要空内容不炸():
    assert make_user_preview("", PREVIEW_MAX_CHARS) == ""


# ==================== 决策 #52 的内容模型 ====================


def hermes_like(system_chars: int, question: str = "帮我看下这个订单为什么没发货") -> list:
    """一次 Hermes agent 调用的形状：巨长的系统提示在前，用户那句话在最后。"""
    return [
        {"role": "system", "content": "You are Hermes Agent. " + "指令细则。" * system_chars},
        {"role": "user", "content": "上一轮的问题"},
        {"role": "assistant", "content": "上一轮的回答"},
        {"role": "user", "content": question},
    ]


def test_用户那句话不再和系统提示抢额度():
    """这是换内容模型最直接的收益：系统提示再长也挤不掉用户的提问。

    系统提示故意取到**超过整个额度**（老模型下用户输入会被压成 0 字）——
    真实记录里 13943 字的系统提示就是这么把 12167 字那条记录的提问吃掉的。
    """
    messages = hermes_like(5000)

    assert len(messages[0]["content"]) > CONTENT_MAX_CHARS  # 前提成立，否则这条测了个寂寞

    shaped = shape_audit_content(messages)

    assert shaped.user_input == "帮我看下这个订单为什么没发货"
    assert shaped.truncated is False


def test_系统提示只留指纹和字数_正文一个字不存():
    messages = hermes_like(2000)
    prompt = messages[0]["content"]

    shaped = shape_audit_content(messages)

    assert shaped.system_prompt_chars == len(prompt)
    assert len(shaped.system_prompt_sha256) == 64
    # 指纹里当然不会有正文，但连字数之外的痕迹也不该有
    assert prompt[:50] not in str(shaped)


def test_同一版提示词指纹相同_改一个字就变():
    """指纹要回答的唯一问题就是「是不是同一版」，这条就是那个问题本身。"""
    a = shape_audit_content(hermes_like(100))
    b = shape_audit_content(hermes_like(100))
    c = shape_audit_content(
        [{"role": "system", "content": "You are Hermes Agent. 指令细则。" * 100 + "!"},
         {"role": "user", "content": "问题"}]
    )

    assert a.system_prompt_sha256 == b.system_prompt_sha256
    assert a.system_prompt_sha256 != c.system_prompt_sha256


def test_没有系统提示时存None而不是空串的指纹():
    """空串的 sha256 是个看起来很正常的 64 位十六进制，一眼分不出是「没有」。"""
    shaped = shape_audit_content([{"role": "user", "content": "你好"}])

    assert shaped.system_prompt_sha256 is None
    assert shaped.system_prompt_chars == 0


def test_多条系统提示一起算指纹():
    """只按第一条算的话，第二条变了指纹却不变 —— 那这个指纹就答不出问题了。"""
    one = shape_audit_content([
        {"role": "system", "content": "A"},
        {"role": "user", "content": "问题"},
    ])
    two = shape_audit_content([
        {"role": "system", "content": "A"},
        {"role": "system", "content": "B"},
        {"role": "user", "content": "问题"},
    ])

    assert one.system_prompt_sha256 != two.system_prompt_sha256
    assert system_prompt_text([
        {"role": "system", "content": "A"}, {"role": "system", "content": "B"},
    ]) == "A\nB"


def test_工具链只留骨架_哪个工具各多长():
    messages = [
        {"role": "user", "content": "列一下桌面文件"},
        {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "list_dir"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "内部报价表.xlsx\n" * 500},
        {"role": "user", "content": "第二个是什么"},
    ]

    entries = parse_tool_skeleton(tool_skeleton(messages))

    assert entries == [{"name": "list_dir", "chars": len("内部报价表.xlsx\n" * 500)}]
    # ⚠️ 知情取舍：正文一个字都不留，只剩体量
    assert "内部报价表" not in (tool_skeleton(messages) or "")


def test_工具名优先取消息自带的name():
    messages = [{"role": "tool", "name": "read_file", "content": "x"}]

    assert parse_tool_skeleton(tool_skeleton(messages)) == [{"name": "read_file", "chars": 1}]


def test_名字取不到时落unknown而不是丢掉这次调用():
    """宁可记成 unknown，也不能让「灌了多少数据进去」这个信号整条消失。

    ⚠️ 断言打在**写入侧产出的原始 JSON** 上。`parse_tool_skeleton` 自己也会把
    空名字兜成 unknown —— 只测它的话，写入侧写空、读取侧洗白，两边都错了这条
    用例照样绿。这就是「测函数不等于测到那条路」的又一个形状。
    """
    messages = [{"role": "tool", "tool_call_id": "没有对应的 assistant", "content": "y" * 42}]

    assert json.loads(tool_skeleton(messages)) == [{"name": "unknown", "chars": 42}]
    assert parse_tool_skeleton(tool_skeleton(messages)) == [{"name": "unknown", "chars": 42}]


def test_没有工具时存None而不是空数组():
    """`IS NULL` 一眼看出「这次没用工具」，空数组要解析完才知道。"""
    assert tool_skeleton(hermes_like(10)) is None
    assert shape_audit_content(hermes_like(10)).tool_skeleton is None


def test_工具条数过多时限条数_不切字符串():
    """⚠️ 这一列是 JSON。切字符串会切出半截数组，正是当年那个 bug 的形状。"""
    from services.audit_content import TOOL_SKELETON_MAX_ENTRIES

    messages = [
        {"role": "tool", "name": f"t{i}", "content": "x"}
        for i in range(TOOL_SKELETON_MAX_ENTRIES + 30)
    ]

    raw = tool_skeleton(messages)
    entries = parse_tool_skeleton(raw)

    assert json.loads(raw)  # 仍然是能解析的数组，这才是重点
    assert len(entries) == TOOL_SKELETON_MAX_ENTRIES + 1
    assert "还有 30 次" in entries[-1]["name"]


def test_超长粘贴仍然保头也保尾():
    """正常提问够不着上限，但粘一整份文档进来时 elide_middle 还得干活。"""
    question = "这个工作流为什么报错？"
    messages = [{"role": "user", "content": "文档正文。" * 8000 + question}]

    shaped = shape_audit_content(messages)

    assert shaped.truncated is True
    assert len(shaped.user_input) <= CONTENT_MAX_CHARS
    assert shaped.user_input.startswith("文档正文。")
    assert shaped.user_input.endswith(question)


def test_坏掉的工具骨架当成没有工具_而不是让详情页500():
    for raw in ["{不是JSON", "null", '{"name": "x"}', '["纯字符串"]', ""]:
        assert parse_tool_skeleton(raw) == []


def test_骨架里的字数是脏数据时收敛成0():
    assert parse_tool_skeleton('[{"name": "t", "chars": "很多"}]') == [{"name": "t", "chars": 0}]
    assert parse_tool_skeleton('[{"name": "t", "chars": -5}]') == [{"name": "t", "chars": 0}]
