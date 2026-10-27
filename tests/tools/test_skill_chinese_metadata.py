import json
from pathlib import Path

import pytest

from tools import skill_chinese_metadata as chinese


def test_ai_fills_missing_chinese_fields_without_changing_slug_or_instructions(tmp_path, monkeypatch):
    path = tmp_path / "SKILL.md"
    source = "---\nname: invoice-helper\ndescription: Parse invoices\n---\n\n# Steps\nRead the invoice and extract totals.\n"
    path.write_text(source)
    monkeypatch.setattr(chinese, "_translate", lambda rows: {"skill": {"name": "发票助手", "description": "识别发票并提取金额，便于核对报销数据"}})
    assert chinese.ensure_skill_metadata(tmp_path) is True
    text = path.read_text()
    assert "name: invoice-helper\n" in text
    assert 'display_name: "发票助手"' in text
    assert text.split("\n---\n", 1)[1] == source.split("\n---\n", 1)[1]


def test_ai_failure_does_not_modify_or_upload_a_skill(tmp_path, monkeypatch):
    path = tmp_path / "SKILL.md"
    source = "---\nname: demo\ndescription: English description\n---\n\nRead only.\n"
    path.write_text(source)
    def fail(_): raise RuntimeError("unavailable")
    monkeypatch.setattr(chinese, "_translate", fail)
    with pytest.raises(ValueError, match="尚未上传"):
        chinese.ensure_skill_metadata(tmp_path)
    assert path.read_text() == source


def test_english_metadata_cannot_pass_by_appending_one_chinese_word():
    assert not chinese.has_chinese("This is a long English introduction 中文")
    assert chinese.has_chinese("Git 项目助手")
    assert not chinese.has_chinese("Use when 在任意电脑离线跑库存管控优化分析。")


@pytest.fixture
def branded_display(tmp_path, monkeypatch):
    import hermes_cli.product_policy as policy
    monkeypatch.setattr(policy, "load_product_policy", lambda: object())
    monkeypatch.setattr(chinese, "get_hermes_home", lambda: tmp_path)
    def unexpected_ai(_):
        pytest.fail("Browsing skills must not call AI")
    monkeypatch.setattr(chinese, "_translate", unexpected_ai)
    return tmp_path


def test_missing_local_metadata_is_honest_and_never_triggers_ai(branded_display):
    source = {"name": "custom-daily-report", "description": "My own purpose", "usage": 7}
    for _ in range(4):
        row = chinese.localize_rows([source], title_field="display_name")[0]
        assert row["name"] == source["name"] and row["usage"] == 7
        assert row["metadata_incomplete"] and not row["metadata_pending"]
        assert "正在生成" not in row["display_name"]
        # 没有中文时显示原文，不用占位句盖掉（2026-10-16）
        assert row["display_name"] == "custom-daily-report" and row["description"] == "My own purpose"
    assert source["description"] == "My own purpose"
    assert not (branded_display / "cache").exists()


def test_existing_chinese_metadata_is_authoritative(branded_display):
    source = {"name": "own-skill", "display_name": "自己的技能", "description": "处理自己定义的业务流程"}
    row = chinese.localize_rows([source], title_field="display_name")[0]
    assert row["display_name"] == source["display_name"]
    assert row["description"] == source["description"]
    assert not row["metadata_incomplete"]


def test_old_successful_cache_remains_readable_but_edits_do_not_reuse_it(branded_display):
    import hashlib
    title, description = "custom-daily-report", "Daily report"
    key = hashlib.sha256(json.dumps([title, description], ensure_ascii=False).encode()).hexdigest()
    cache = branded_display / "cache" / "skill-metadata-zh.json"
    cache.parent.mkdir()
    cache.write_text(json.dumps({key: {"name": "日报助手", "description": "生成业务日报"}}))
    row = chinese.localize_rows([{"name": title, "description": description}])[0]
    assert row["name"] == "日报助手" and not row["metadata_incomplete"]
    changed = chinese.localize_rows([{"name": title, "description": "Changed purpose"}])[0]
    assert changed["metadata_incomplete"]
