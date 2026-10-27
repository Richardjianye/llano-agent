"""个人技能「整理并发布」（2026-10-16，P2）。

真实临时 HERMES_HOME、真实打包/扫描/备份/回执；网关替换在 HTTP 边界
（`tools.llano_skill_hub._request`），模型替换在辅助模型调用边界（`call_llm`）。
"""
import json
import types

import pytest

ORIGINAL = (
    "---\nname: weekly-report\ndescription: Weekly sales summary for me\n---\n\n"
    "1. 打开 /Users/developer/reports\n2. 给 13812345678 打电话确认华东客户 ACME 的 128,000 元订单\n"
)


@pytest.fixture
def env(monkeypatch, _isolate_hermes_home):
    import hermes_cli.product_policy as policy
    from hermes_constants import get_hermes_home
    from tools import llano_skill_hub, skill_manager_tool, skills_hub

    home = get_hermes_home()
    skill = home / "skills" / "weekly-report"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(ORIGINAL, encoding="utf-8")
    (skill / "notes.md").write_text("参考资料，无敏感信息\n", encoding="utf-8")
    monkeypatch.setattr(skill_manager_tool, "SKILLS_DIR", home / "skills")
    monkeypatch.setattr(skills_hub, "SKILLS_DIR", home / "skills")
    monkeypatch.setattr(policy, "load_product_policy", lambda: types.SimpleNamespace(sync_skill_usage=True))

    state = types.SimpleNamespace(calls=[], published=[], status={"status": "available"}, home=home, skill=skill)

    def gateway(path, *, method="GET", body=None):
        state.calls.append((method, path))
        if path.startswith("/v1/skill-hub/preflight/"):
            return {"slug": path.rsplit("/", 1)[1], **state.status}
        if path == "/v1/skill-hub/skills" and method == "POST":
            payload = json.loads(body)
            state.published.append(payload)
            return {"created": True, "slug": payload["slug"], "version": 1, "scope": "dept",
                    "name": payload["name"], "description": payload["description"], "bundle_hash": "h1"}
        raise AssertionError(path)

    from tools import skill_publish

    monkeypatch.setattr(llano_skill_hub, "_request", gateway)
    monkeypatch.setattr(skill_publish, "_request", gateway)  # 模块加载时已绑定
    monkeypatch.setattr(llano_skill_hub, "hub_origin", lambda: "https://gateway.test")
    return state


def _model(monkeypatch, reply=None, error=None):
    import agent.auxiliary_client as aux

    seen = []

    def call_llm(**kwargs):
        seen.append(kwargs)
        if error:
            raise error
        return reply

    monkeypatch.setattr(aux, "call_llm", call_llm)
    monkeypatch.setattr(aux, "extract_content_or_reasoning", lambda response: response)
    return seen


AI_REPLY = json.dumps({
    "display_name": "周销售汇总",
    "description": "整理每周销售数据并确认大额订单时使用。",
    "body": "## 适用场景\n每周汇总销售。\n\n## 步骤\n1. 打开 {用户目录}/reports\n2. 给 {手机号} 打电话确认{客户名}的订单（待作者确认）\n",
    "sensitive": [{"text": "华东客户 ACME", "reason": "客户名", "placeholder": "{客户名}"},
                  {"text": "模型编出来的", "reason": "x", "placeholder": "{x}"}],
    "changelog": "首次发布",
}, ensure_ascii=False)


def test_rule_scan_finds_personal_data_without_ai():
    from tools.skill_refine import scan_sensitive

    text = ("电话 13812345678，身份证 110101199003071234，卡号 6222020200112233445，"
            "mail a.b@corp.com，api_key: abcdef123456，服务器 192.168.1.20，"
            r"路径 C:\Users\lisi\x 与 /home/wang/y，版本 v2.10.3")
    kinds = {f["kind"]: f["text"] for f in scan_sensitive({"SKILL.md": text})}
    assert kinds["phone"] == "13812345678" and kinds["id_card"] == "110101199003071234"
    assert kinds["bank_card"] == "6222020200112233445" and kinds["email"] == "a.b@corp.com"
    assert kinds["secret"] == "api_key: abcdef123456", "止于中文标点，不吞后文"
    paths = [f["text"] for f in scan_sensitive({"SKILL.md": text}) if f["kind"] == "user_path"]
    assert kinds["private_ip"] == "192.168.1.20" and any("lisi" in p for p in paths) and "/home/wang" in paths
    assert not any("2.10.3" in f["text"] for f in scan_sensitive({"SKILL.md": text}))


def test_machine_check_runs_without_ai_and_flags_spec_gaps(env, monkeypatch):
    from tools.skill_refine import check_publish

    seen = _model(monkeypatch, AI_REPLY)
    check = check_publish("weekly-report")

    assert seen == [], "机器检查不调模型"
    assert check["status"]["status"] == "available" and check["slug"] == "weekly-report"
    assert ("GET", "/v1/skill-hub/preflight/weekly-report") in env.calls
    texts = {f["text"] for f in check["findings"]}
    assert {"13812345678", "/Users/zhangsan"} <= texts
    rules = {item["rule"] for item in check["lint"]}
    assert {"missing-display-name", "missing-publish-sections", "personal-data"} <= rules
    assert check["conforms"] is False and check["cached"] is False
    assert (env.skill / "SKILL.md").read_text(encoding="utf-8") == ORIGINAL, "检查只读"


def test_a_skill_written_to_the_spec_passes_the_machine_check(env):
    from tools.skill_publish_spec import SECTIONS
    from tools.skill_refine import check_publish

    body = "".join(f"## {section}\n内容\n\n" for section in SECTIONS)
    (env.skill / "SKILL.md").write_text(
        f'---\nname: weekly-report\ndisplay_name: "周销售汇总"\ndescription: 整理每周销售数据时使用。\n---\n\n{body}',
        encoding="utf-8")
    assert check_publish("weekly-report")["conforms"] is True


def test_ai_draft_is_reviewable_and_drops_invented_findings(env, monkeypatch):
    from tools.skill_refine import refine_publish

    seen = _model(monkeypatch, AI_REPLY)
    draft = refine_publish("weekly-report")

    assert seen[0]["task"] == "skill_refine" and "13812345678" in seen[0]["messages"][1]["content"]
    assert 'display_name: "周销售汇总"' in draft["draft"] and "name: weekly-report" in draft["draft"]
    assert "## 步骤" in draft["draft"] and "128,000" not in draft["draft"]
    texts = {f["text"]: f["source"] for f in draft["findings"]}
    assert texts["13812345678"] == "rule" and texts["/Users/zhangsan"] == "rule"
    assert texts["华东客户 ACME"] == "ai" and "模型编出来的" not in texts, "AI 编的、原文没有的不收"
    assert draft["changelog"] == "首次发布" and draft["ai_error"] == ""
    assert (env.skill / "SKILL.md").read_text(encoding="utf-8") == ORIGINAL, "草稿一步只读"
    assert env.published == []


def test_review_results_are_reused_until_the_skill_changes_or_a_refresh(env, monkeypatch):
    from tools.skill_refine import check_publish, refine_publish

    seen = _model(monkeypatch, AI_REPLY)
    first_check, first_draft = check_publish("weekly-report"), refine_publish("weekly-report")
    preflights = len(env.calls)

    again_check, again_draft = check_publish("weekly-report"), refine_publish("weekly-report")
    assert again_check["cached"] and again_draft["cached"]
    assert len(seen) == 1 and len(env.calls) == preflights, "没改动就不重复扫描和调 AI"
    assert again_draft["draft"] == first_draft["draft"]
    assert again_check["checked_at"] == first_check["checked_at"]

    assert not refine_publish("weekly-report", refresh=True)["cached"] and len(seen) == 2
    assert not check_publish("weekly-report", refresh=True)["cached"]

    (env.skill / "notes.md").write_text("改过的参考资料\n", encoding="utf-8")
    assert not check_publish("weekly-report")["cached"], "技能文件变了就重新检查"
    assert not refine_publish("weekly-report")["cached"] and len(seen) == 3


def test_ai_failure_falls_back_to_the_original_and_is_retried_next_time(env, monkeypatch):
    from tools.skill_refine import refine_publish

    _model(monkeypatch, error=RuntimeError("model down"))
    draft = refine_publish("weekly-report")
    assert draft["draft"] == ORIGINAL and draft["ai_error"]
    assert {f["text"] for f in draft["findings"]} >= {"13812345678", "/Users/zhangsan"}

    seen = _model(monkeypatch, AI_REPLY)
    assert not refine_publish("weekly-report")["cached"] and len(seen) == 1, "失败的结果不缓存"


def test_unhandled_sensitive_data_blocks_publishing(env):
    from tools.skill_refine import publish_refined

    with pytest.raises(ValueError, match="还有 2 处敏感信息没处理"):
        publish_refined("weekly-report", ORIGINAL)
    assert env.published == []


def test_publishing_the_reviewed_draft_replaces_the_local_skill_after_a_backup(env, monkeypatch):
    from tools.skill_publish import published_receipt
    from tools.skill_refine import check_publish, publish_refined, publish_states

    _model(monkeypatch, AI_REPLY)
    check_publish("weekly-report")

    reviewed = ORIGINAL.replace("/Users/zhangsan", "{用户目录}").replace("13812345678", "{手机号}")
    result = publish_refined("weekly-report", reviewed, changelog="首次发布", kept=["128,000"])

    assert result["success"] and result["replaced_local"]
    sent = {f["path"]: f["content"] for f in env.published[0]["files"]}
    assert sent["SKILL.md"] == reviewed and "notes.md" in sent
    assert env.published[0]["changelog"] == "首次发布"
    assert (env.skill / "SKILL.md").read_text(encoding="utf-8") == reviewed
    from pathlib import Path
    backup = Path(result["local_backup"])
    assert (backup / "SKILL.md").read_text(encoding="utf-8") == ORIGINAL

    rows = [{"name": "weekly-report", "provenance": "agent", "usage": 9}]
    publish_states(rows)
    assert rows[0]["publish_state"] == {"published": True, "slug": "weekly-report",
                                        "local_changed": False, "suggest": False}
    assert published_receipt("weekly-report")["slug"] == "weekly-report"
    after = check_publish("weekly-report")
    assert not after["cached"], "发布后状态变了，不能沿用发布前的检查结果"

    (env.skill / "SKILL.md").write_text(reviewed + "3. 后台复盘补了一步\n", encoding="utf-8")
    publish_states(rows)
    assert rows[0]["publish_state"]["local_changed"], "发布后又改过"


def test_a_taken_slug_can_be_published_under_a_new_one_and_stats_follow_it(env):
    from tools.skill_publish import published_receipt
    from tools.skill_refine import publish_refined

    reviewed = ORIGINAL.replace("/Users/zhangsan", "{用户目录}").replace("13812345678", "{手机号}")
    publish_refined("weekly-report", reviewed, slug="weekly-report-east")
    assert env.published[0]["slug"] == "weekly-report-east"
    assert published_receipt("weekly-report")["slug"] == "weekly-report-east"


def test_the_skill_identifier_cannot_be_renamed_through_refining(env):
    from tools.skill_refine import publish_refined

    with pytest.raises(ValueError, match="不能修改技能标识"):
        publish_refined("weekly-report", "---\nname: other\ndescription: x\n---\n正文\n")


def test_frequently_used_unpublished_skills_are_suggested_for_sharing(env):
    from tools.skill_refine import publish_states

    rows = [{"name": "weekly-report", "provenance": "agent", "usage": 3},
            {"name": "rarely", "provenance": "imported", "usage": 1},
            {"name": "shipped", "provenance": "bundled", "usage": 50}]
    publish_states(rows)
    assert rows[0]["publish_state"]["suggest"] and not rows[1]["publish_state"]["suggest"]
    assert "publish_state" not in rows[2], "随包技能不提示发布"


def test_a_dismissed_share_hint_stays_dismissed(env, monkeypatch):
    from starlette.testclient import TestClient

    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app
    from tools.skill_refine import publish_states

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    assert client.post("/api/skills/publish/dismiss-suggestion", json={"name": "weekly-report"}).status_code == 200

    rows = [{"name": "weekly-report", "provenance": "agent", "usage": 9},
            {"name": "other", "provenance": "agent", "usage": 9}]
    publish_states(rows)
    assert not rows[0]["publish_state"]["suggest"] and rows[1]["publish_state"]["suggest"]


def test_rest_endpoints_map_errors_for_the_ui(env, monkeypatch):
    from starlette.testclient import TestClient

    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app

    _model(monkeypatch, AI_REPLY)
    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    checked = client.post("/api/skills/publish/check", json={"name": "weekly-report"})
    assert checked.status_code == 200 and checked.json()["status"]["status"] == "available"
    refined = client.post("/api/skills/publish/refine", json={"name": "weekly-report"})
    assert refined.status_code == 200 and "周销售汇总" in refined.json()["draft"]
    again = client.post("/api/skills/publish/refine", json={"name": "weekly-report", "refresh": True})
    assert again.status_code == 200 and again.json()["cached"] is False

    blocked = client.post("/api/skills/publish/refined", json={"name": "weekly-report", "skill_md": ORIGINAL})
    assert blocked.status_code == 400 and "敏感信息" in blocked.json()["detail"]
    missing = client.post("/api/skills/publish/check", json={"name": "nope"})
    assert missing.status_code == 400
