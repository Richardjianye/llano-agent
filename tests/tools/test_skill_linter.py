"""Tests for tools/skill_linter.py — the advisory SKILL.md convention linter."""

from pathlib import Path

import pytest

from tools.skill_linter import (
    ERROR,
    WARNING,
    format_findings,
    has_errors,
    lint_content,
    lint_skill,
)

# A clean, peer-shaped SKILL.md that should produce zero findings.
CLEAN = """---
name: my-skill
description: Search arXiv papers by keyword, author, or ID.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [arxiv, research]
    related_skills: []
---

# My Skill

## Overview
Does a thing.

## When to Use
- When the user wants X.

## Procedure
1. Use `read_file` to load it.
"""


def _rules(findings):
    return {f.rule for f in findings}


def test_clean_skill_has_no_findings():
    assert lint_content(CLEAN) == []


def test_description_too_long_is_warning():
    long_desc = "x" * 80
    content = CLEAN.replace(
        "Search arXiv papers by keyword, author, or ID.", long_desc
    )
    findings = lint_content(content)
    assert "description-length" in _rules(findings)
    assert all(f.severity == WARNING for f in findings)


def test_marketing_words_flagged():
    content = CLEAN.replace(
        "Search arXiv papers by keyword, author, or ID.",
        "A powerful comprehensive tool.",
    )
    findings = lint_content(content)
    assert "description-marketing" in _rules(findings)


def test_shell_utility_reference_in_prose_flagged():
    content = CLEAN.replace("Use `read_file` to load it.", "Use `grep` to find it.")
    findings = lint_content(content)
    assert "shell-utility-reference" in _rules(findings)


def test_shell_utility_inside_code_block_not_flagged():
    # A fenced code block legitimately shows grep; prose check must skip it.
    content = CLEAN + "\n```bash\ngrep -r foo .\n```\n"
    findings = lint_content(content)
    assert "shell-utility-reference" not in _rules(findings)


def test_missing_metadata_block_warns():
    content = """---
name: bare-skill
description: Does a thing briefly.
---

# Bare Skill

## When to Use
- now
"""
    findings = lint_content(content)
    rules = _rules(findings)
    assert "missing-metadata" in rules


def test_missing_when_to_use_section_warns():
    content = CLEAN.replace("## When to Use\n- When the user wants X.\n", "")
    findings = lint_content(content)
    assert "missing-section" in _rules(findings)


def test_bad_name_format_is_error():
    content = CLEAN.replace("name: my-skill", "name: My_Skill!")
    findings = lint_content(content)
    assert "name-format" in _rules(findings)
    assert has_errors(findings)


def test_name_dir_mismatch_is_error(tmp_path):
    skill_dir = tmp_path / "actual-dir"
    skill_dir.mkdir()
    findings = lint_content(CLEAN, skill_dir=skill_dir)  # name is my-skill
    assert "name-dir-mismatch" in _rules(findings)
    assert has_errors(findings)


def test_dangling_reference_link_flagged(tmp_path):
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    content = CLEAN + "\nSee references/missing.md for detail.\n"
    findings = lint_content(content, skill_dir=skill_dir)
    assert "dangling-reference" in _rules(findings)


def test_present_reference_link_not_flagged(tmp_path):
    skill_dir = tmp_path / "my-skill"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "references" / "detail.md").write_text("x")
    content = CLEAN + "\nSee references/detail.md for detail.\n"
    findings = lint_content(content, skill_dir=skill_dir)
    assert "dangling-reference" not in _rules(findings)


def test_posix_primitive_without_platforms_warns(tmp_path):
    skill_dir = tmp_path / "my-skill"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "run.py").write_text("import fcntl\nfcntl.flock(1, 2)\n")
    findings = lint_content(CLEAN, skill_dir=skill_dir)
    assert "platforms-gating" in _rules(findings)


def test_posix_primitive_with_platforms_ok(tmp_path):
    skill_dir = tmp_path / "my-skill"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "run.py").write_text("import fcntl\n")
    content = CLEAN.replace(
        "version: 1.0.0", "version: 1.0.0\nplatforms: [linux, macos]"
    )
    findings = lint_content(content, skill_dir=skill_dir)
    assert "platforms-gating" not in _rules(findings)


def test_forbidden_file_flagged(tmp_path):
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "README.md").write_text("# readme")
    findings = lint_content(CLEAN, skill_dir=skill_dir)
    assert "forbidden-file" in _rules(findings)


def test_invalid_platforms_value_warns():
    content = CLEAN.replace(
        "version: 1.0.0", "version: 1.0.0\nplatforms: [linux, solaris]"
    )
    findings = lint_content(content)
    assert "platforms-value" in _rules(findings)


def test_lint_skill_reads_from_disk(tmp_path):
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(CLEAN)
    findings = lint_skill(skill_md)
    assert findings == []


def test_author_caps_warned():
    content = CLEAN.replace("author: Hermes Agent", "author: hermes agent")
    findings = lint_content(content)
    assert "author-caps" in _rules(findings)


def test_format_findings_renders():
    findings = lint_content(CLEAN.replace("name: my-skill", "name: BAD"))
    out = format_findings(findings)
    assert "name-format" in out


# ── 品牌版展示名规则（CUSTOM，决策 #59）───────────────────────────────────
#
# 这组规则只在创建路径、品牌版开启：随包的英文技能和 CI 不该被它刷屏。

ZH_CLEAN = """---
name: invoice-helper
display_name: 发票助手
description: "处理中文发票识别、校验与导出。触发词：发票、报销。"
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [invoice]
    related_skills: []
---

# 发票助手

## 适用场景
- 同事要识别或核对发票时。

## 前置条件
- 发票图片或 PDF。

## 步骤
1. Use `read_file` to load the invoice.

## 注意事项
- 金额以发票原件为准。

## 怎么验证
- 识别出的字段与原件一致。
"""


def test_product_rules_are_off_by_default():
    # CLEAN 是英文技能、没有 display_name：默认路径（含 CI、随包技能）一条都不该多出来
    assert lint_content(CLEAN) == []


def test_product_rules_flag_an_english_skill_without_display_name():
    findings = lint_content(CLEAN, product_rules=True)

    assert _rules(findings) == {"missing-display-name", "description-not-chinese", "missing-publish-sections"}
    # 提醒不是拦截：全是 WARNING，has_errors 不该被它带成 True
    assert not has_errors(findings)


def test_product_rules_pass_for_a_chinese_skill():
    assert lint_content(ZH_CLEAN, product_rules=True) == []


def test_product_rules_name_the_missing_publish_sections():
    content = ZH_CLEAN.replace("## 前置条件\n- 发票图片或 PDF。\n\n", "").replace("## 怎么验证", "## 其它")

    (finding,) = lint_content(content, product_rules=True)

    assert finding.rule == "missing-publish-sections"
    assert "前置条件" in finding.message and "怎么验证" in finding.message
    assert "适用场景" not in finding.message.split("：", 1)[1].split("。")[0]


def test_a_heading_with_extra_words_still_counts_as_the_section():
    content = ZH_CLEAN.replace("## 步骤", "## 步骤（每周一次）")
    assert lint_content(content, product_rules=True) == []


def test_product_rules_flag_personal_data_but_not_in_stock_mode():
    content = ZH_CLEAN.replace("发票图片或 PDF。", "发票放在 /Users/developer/invoices，有问题打 13812345678。")

    (finding,) = lint_content(content, product_rules=True)

    assert finding.rule == "personal-data" and "13812345678" in finding.message
    assert not has_errors([finding])
    assert "personal-data" not in _rules(lint_content(content))


def test_display_name_must_be_chinese():
    content = ZH_CLEAN.replace("display_name: 发票助手", "display_name: Invoice Helper")

    findings = lint_content(content, product_rules=True)

    assert _rules(findings) == {"display-name-not-chinese"}


def test_display_name_too_long_is_reported():
    content = ZH_CLEAN.replace("发票助手", "这是一个名字特别特别特别特别长的技能展示名称")

    assert _rules(lint_content(content, product_rules=True)) == {"display-name-length"}


def test_the_message_carries_a_copyable_patch():
    # 消息要能直接照抄：模型看到 display_name: "发票助手" 这种成品比看到一句「请补齐」
    # 少跑一轮
    (finding,) = [
        f for f in lint_content(CLEAN, product_rules=True) if f.rule == "missing-display-name"
    ]

    assert 'display_name: "发票助手"' in finding.message
    assert "skill_manage(action='patch')" in finding.message


def test_cli_product_flag(tmp_path, capsys):
    from tools.skill_linter import _main

    skill = tmp_path / "invoice-helper"
    skill.mkdir()
    (skill / "SKILL.md").write_text(CLEAN.replace("my-skill", "invoice-helper"), encoding="utf-8")

    assert _main([str(skill)]) == 0
    assert "missing-display-name" not in capsys.readouterr().out

    assert _main(["--product", str(skill)]) == 0  # 只有 WARNING，退出码仍是 0
    assert "missing-display-name" in capsys.readouterr().out
