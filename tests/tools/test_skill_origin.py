"""技能来源标记与业务数据扫描（P0-4，阶段 14）

P0-4 原话：「这是离职处理的前置条件，**事后拆不出来**。」所以这一组用例守的是
两件事：

1. **标记在创建那一刻就写下来，而且不被后续编辑改掉**
2. **「没有标记」不等于「个人」** —— 它是「不知道」，不能替管理员做那个判断

⚠️ 今天客户端还没有项目上下文，所以生产路径上每条标记都会是 personal。
   这不是空转：「知道这 200 条是个人的」和「不知道」是实质差别。项目上下文接上来
   那天，要改的只有 current_project() 一个函数。
"""

from __future__ import annotations

import json

import pytest

from tools import skill_origin
from tools.skill_data_scan import scan_text, summarize
from tools.skill_origin import (PERSONAL, PROJECT, ProjectContext,
                                group_for_offboarding, read, stamp)


@pytest.fixture
def skills_root(tmp_path):
    return tmp_path / "skills"


def _make(root, name, *, category=""):
    d = root / category / name if category else root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n\n# {name}\n", encoding="utf-8")
    return d


# ── 标记的写入 ────────────────────────────────────────────────────────────

def test_a_skill_created_outside_a_project_is_marked_personal(skills_root):
    """基线，也是**今天生产路径上唯一会走到的那一条**。"""
    d = _make(skills_root, "my-habit")
    stamp(d)

    record = read(d)
    assert record["scope"] == PERSONAL
    assert "project_code" not in record


def test_a_skill_created_inside_a_project_carries_the_project(skills_root):
    d = _make(skills_root, "q4-report-howto")
    token = skill_origin.set_current_project(
        ProjectContext(project_code="q4-report", project_name="Q4 报表", session_id="s-1")
    )
    try:
        stamp(d)
    finally:
        skill_origin.reset_current_project(token)

    record = read(d)
    assert record["scope"] == PROJECT
    assert record["project_code"] == "q4-report"
    assert record["session_id"] == "s-1"


def test_stamping_twice_does_not_rewrite_the_origin(skills_root):
    """改一条已存在的技能不该改写它的出处。

    一条产生于 Q4 项目的技能，后来在私聊里被顺手改过一次，它**仍然产生于那个
    项目**。覆盖的话，离职当天那条会被算成个人的 —— 而这正是 P0-4 要防的
    「事后拆不出来」。
    """
    d = _make(skills_root, "q4-report-howto")
    token = skill_origin.set_current_project(ProjectContext(project_code="q4-report"))
    try:
        stamp(d)
    finally:
        skill_origin.reset_current_project(token)

    # 后来在个人上下文里又改了一次
    stamp(d)

    assert read(d)["project_code"] == "q4-report"


def test_the_project_context_does_not_leak_across_turns(skills_root):
    """ContextVar 必须 reset 干净，否则一轮项目对话之后，后面所有个人技能
    都会被打上项目标记 —— 而那会把个人数据错误地划进项目资产。"""
    token = skill_origin.set_current_project(ProjectContext(project_code="q4-report"))
    skill_origin.reset_current_project(token)

    d = _make(skills_root, "later-personal")
    stamp(d)

    assert read(d)["scope"] == PERSONAL


def test_a_failed_stamp_does_not_raise(skills_root, monkeypatch):
    """标记打不上是要修的问题，但不该让创建技能这件事失败。"""
    d = _make(skills_root, "x")

    def boom(*a, **kw):
        raise OSError("磁盘满了")

    monkeypatch.setattr("pathlib.Path.write_text", boom)

    assert stamp(d) is None


# ── 离职分组 ──────────────────────────────────────────────────────────────

def test_unmarked_skills_are_unknown_not_personal(skills_root):
    """**「没有标记」是「不知道」，不是「个人」。**

    并进 personal 是最省事的写法，也是错的：那等于替管理员断言「这些不是项目
    资产」，而他根本没看过。阶段 14 之前建的技能全都没有标记。
    """
    _make(skills_root, "old-skill")  # 不打标记
    stamped = _make(skills_root, "new-skill")
    stamp(stamped)

    grouped = group_for_offboarding(skills_root)

    assert [s["name"] for s in grouped["unknown"]] == ["old-skill"]
    assert [s["name"] for s in grouped["personal"]] == ["new-skill"]


def test_project_skills_are_grouped_by_project(skills_root):
    for name, code in [("a", "q4-report"), ("b", "q4-report"), ("c", "hr-tools")]:
        d = _make(skills_root, name)
        token = skill_origin.set_current_project(ProjectContext(project_code=code))
        try:
            stamp(d)
        finally:
            skill_origin.reset_current_project(token)

    grouped = group_for_offboarding(skills_root)

    assert sorted(grouped["by_project"]) == ["hr-tools", "q4-report"]
    assert len(grouped["by_project"]["q4-report"]) == 2
    assert grouped["personal"] == []


def test_nested_categories_are_found(skills_root):
    """技能按 category/skill 分层。只扫一层的话，分类下的技能全部漏掉 ——
    而漏掉的表现是「离职清单看起来很干净」。"""
    d = _make(skills_root, "deep-one", category="ops/reports")
    stamp(d)

    grouped = group_for_offboarding(skills_root)

    assert grouped["total"] == 1
    assert grouped["personal"][0]["path"] == "ops/reports/deep-one"


def test_a_corrupt_marker_counts_as_unknown(skills_root):
    """标记文件坏了要当「不知道」，不能当「个人」—— 同上面那条理由。"""
    d = _make(skills_root, "broken")
    (d / ".llano-origin.json").write_text("{不是 JSON", encoding="utf-8")

    grouped = group_for_offboarding(skills_root)

    assert [s["name"] for s in grouped["unknown"]] == ["broken"]


def test_an_empty_tree_is_not_an_error(skills_root):
    assert group_for_offboarding(skills_root)["total"] == 0


# ── 业务数据扫描 ──────────────────────────────────────────────────────────

def test_a_clean_skill_produces_nothing():
    """基线。没有这条的话，下面的「报出来了」可能只是因为它什么都报。"""
    text = (
        "# 查平台销量\n\n"
        "1. 调 `lx.report.platform.list`，传 `start_date` / `end_date`\n"
        "2. 返回里取 `total_amount` 字段\n"
        "3. 注意分页，`page` 从 1 开始\n"
    )

    assert scan_text(text) == []


def test_placeholders_are_not_flagged():
    """占位符必须先被排掉。

    正确写法和错误写法一样吵的话，人很快会把这类提示全部忽略 —— 包括真的那些。
    """
    text = (
        "GET /order/<order_id>\n"
        "响应：{\"amount\": <金额>, \"asin\": \"{asin}\"}\n"
        "订单号形如 xxxxxxxxxxxx\n"
        "示例：${ORDER_ID}\n"
    )

    assert scan_text(text) == []


def test_a_real_order_id_is_flagged():
    hits = scan_text("查这个订单：114-2938475-1029384，订单号 20260829112233")

    assert any(h.kind == "long_digits" for h in hits)


def test_a_real_asin_is_flagged():
    """ASIN 对这家公司信号极强 —— 领星的接口返回里到处是它。"""
    hits = scan_text("商品 B0CZ8K7QRT 的销量")

    assert any(h.kind == "asin" for h in hits)


def test_money_amounts_are_flagged():
    for text in ["合计 ¥12,345.60", "总额 1234.56 元", "$999.99"]:
        assert any(h.kind == "money" for h in scan_text(text)), text


def test_a_pasted_result_table_is_flagged():
    """「贴回来的结果表」是最典型的一种：agent 把查询结果原样抄进 skill。"""
    text = (
        "| 店铺 | 销量 | 金额 |\n"
        "| --- | --- | --- |\n"
        "| 店A | 1203 | 45600 |\n"
    )
    hits = scan_text(text)

    assert any(h.kind == "result_table" for h in hits)


def test_a_schema_table_is_not_flagged():
    """反向：字段说明表不该被报出来，那是 skill 该有的内容。"""
    text = (
        "| 字段 | 类型 | 说明 |\n"
        "| --- | --- | --- |\n"
        "| asin | string | 商品编码 |\n"
    )

    assert scan_text(text) == []


def test_a_pasted_response_body_is_flagged():
    text = (
        "```json\n"
        '{"seller_name": "深圳某某贸易有限公司", "total": 45600}\n'
        "```\n"
    )
    hits = scan_text(text)

    assert any(h.kind in {"response_sample", "long_digits"} for h in hits)


def test_findings_carry_line_numbers_so_a_human_can_look():
    """只报「有问题」而不说在哪，等于让人重读全文。"""
    hits = scan_text("正常一行\n第二行 B0CZ8K7QRT\n")

    assert hits[0].line == 2


def test_the_summary_is_directly_displayable():
    summary = summarize(scan_text("B0CZ8K7QRT 和 ¥1,234.00"))

    assert summary["count"] >= 2
    assert set(summary["by_kind"]) >= {"asin", "money"}
    assert all({"line", "kind", "excerpt", "hint"} <= set(f) for f in summary["findings"])


# ── 接上去了没有 ──────────────────────────────────────────────────────────

def test_creating_a_skill_actually_stamps_it(tmp_path, monkeypatch):
    """守卫写对了不等于接上了 —— 这个仓库栽过两次的地方。

    上面全部用例测的都是 stamp() 本身，没有一条测创建路径。
    """
    import tools.skill_manager_tool as smt

    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(smt, "_skills_dir", lambda: root)

    result = smt._create_skill(
        "stamped-skill",
        "---\nname: stamped-skill\ndescription: Use when testing. Does nothing.\n---\n\n# X\n\n## Steps\n1. none\n",
    )

    assert result.get("success") is True, result
    assert read(root / "stamped-skill") is not None


def test_the_prompt_rule_reaches_the_model():
    """规则原话是「在 skill 生成的提示词层就约束」。

    枚举/描述里没有这句话的话，模型永远不知道有这条规则，扫描就只能一直事后
    补救 —— 而事后补救的东西已经写进磁盘了。
    """
    from tools.skill_manager_tool import SKILL_MANAGE_SCHEMA

    blob = str(SKILL_MANAGE_SCHEMA)

    assert "retention policy" in blob
    assert "placeholder" in blob


def test_the_origin_marker_is_not_published_to_the_hub():
    """来源标记发到全公司只是噪音，而且会把「这个技能产生于哪个项目」带出去 ——
    它是给离职取舍用的，不是给全员看的。"""
    from tools.skill_publish import SKIP_FILES

    assert ".llano-origin.json" in SKIP_FILES
