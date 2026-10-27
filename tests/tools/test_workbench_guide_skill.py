"""随包的「工作台使用指南」技能与它的指路句（2026-09-21）。

09-20 一句「更改登录密码」让模型先去读 hermes-agent（Hermes 开发者手册，里面没有
账号 / 改密码），读不到又照着「没写到先在本机验证」跑了 42 次终端命令、510 秒没回一个字。
这几条守住三件事：指南存在且合格、指路句指向的技能真的存在、hermes-agent 不再教模型翻本机。
"""

from __future__ import annotations

import re
from pathlib import Path

from agent.prompt_builder import HERMES_AGENT_HELP_GUIDANCE
from tools.skill_linter import PRODUCT_RULES, lint_content
from tools.skill_manager_tool import _validate_frontmatter
from agent.skill_utils import parse_frontmatter

REPO_ROOT = Path(__file__).resolve().parents[2]
GUIDE_DIR = REPO_ROOT / "skills" / "autonomous-ai-agents" / "llano-workbench-guide"
AUTHORING_DIR = REPO_ROOT / "skills" / "software-development" / "hermes-agent-skill-authoring"


def _bundled_skill_names() -> set[str]:
    names = set()
    for skill_md in (REPO_ROOT / "skills").rglob("SKILL.md"):
        front, _ = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        names.add(str(front.get("name", "")))
    return names


def test_the_guide_is_a_valid_chinese_first_skill():
    text = (GUIDE_DIR / "SKILL.md").read_text(encoding="utf-8")
    front, _ = parse_frontmatter(text)

    assert front["name"] == GUIDE_DIR.name  # 目录名与 name 必须一致
    assert _validate_frontmatter(text, new_skill=True) is None
    # 指南是随包的问答型技能，不是要发布给同事的个人技能，所以不套发布规范的章节要求；
    # 中文名、中文说明、不含个人信息这几条照样要过。
    rules = {f.rule for f in lint_content(text, skill_dir=GUIDE_DIR, product_rules=True)}
    assert not rules & (PRODUCT_RULES - {"missing-publish-sections"})
    assert re.search(r"[一-鿿]", front["display_name"])
    # 系统提示里只保留描述的前 57 个字，触发词必须落在这个窗口内
    assert len(str(front["description"])) <= 57


def test_the_guide_answers_the_question_that_started_this():
    body = (GUIDE_DIR / "SKILL.md").read_text(encoding="utf-8")

    for needle in ("修改密码", "至少 8 位", "初始密码", "忘记密码", "设置 → 关于", "添加技能", "联系管理员"):
        assert needle in body, needle
    # 指南自己也不能教模型去翻本机
    assert "不要去翻安装目录" in body


def test_the_help_guidance_points_at_skills_that_exist():
    referenced = set(re.findall(r"skill_view\(name='([^']+)'\)", HERMES_AGENT_HELP_GUIDANCE))

    assert "llano-workbench-guide" in referenced
    assert referenced <= _bundled_skill_names()
    # 「没写到就直说不确定」这句不能丢：它是防止再次翻箱倒柜的那道闸
    assert "不确定" in HERMES_AGENT_HELP_GUIDANCE
    assert "不要翻安装目录" in HERMES_AGENT_HELP_GUIDANCE


def test_hermes_agent_no_longer_tells_the_model_to_verify_on_the_machine():
    text = (REPO_ROOT / "skills" / "autonomous-ai-agents" / "hermes-agent" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "Verify locally" not in text
    assert "不要去本机遍历安装目录" in text


def test_the_personal_skill_template_passes_every_check():
    # 模板一旦被改坏（比如去掉引号），同事照抄的技能会全部导入失败，还是英文报错
    template = (AUTHORING_DIR / "templates" / "personal-skill-template.md").read_text(encoding="utf-8")

    assert _validate_frontmatter(template, new_skill=True) is None
    assert not {f.rule for f in lint_content(template, product_rules=True)} & PRODUCT_RULES


def test_the_authoring_skill_teaches_display_name():
    text = (AUTHORING_DIR / "SKILL.md").read_text(encoding="utf-8")
    front, _ = parse_frontmatter(text)

    assert "display_name" in text
    assert front.get("display_name")  # 它自己也得有中文展示名
    assert _validate_frontmatter(text, new_skill=True) is None
