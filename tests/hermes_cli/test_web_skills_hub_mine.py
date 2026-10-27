"""「我发布的技能」与技能卡片上的「我发布的」标记（2026-10-16）。

网关只在 HTTP 边界替身（`tools.llano_skill_hub._request`），本机发布回执、技能目录与
内容指纹都走真实临时 HERMES_HOME。
"""
import json

import pytest


@pytest.fixture
def client(monkeypatch, _isolate_hermes_home):
    from starlette.testclient import TestClient

    from hermes_constants import get_hermes_home
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app
    from tools import llano_skill_hub as hub

    home = get_hermes_home()
    (home / "skills").mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    calls = []

    def gateway(path, **_):
        calls.append(path)
        if path == "/v1/skill-hub/usage-identity":
            return {"uid": "me", "scope": "private"}
        if path == "/v1/skill-hub/mine":
            return {"skills": [
                {"slug": "weekly-report", "name": "周报", "status": "listed", "version": 2},
                {"slug": "old-flow", "name": "旧流程", "status": "delisted", "delisted_reason": "流程已废止"},
            ]}
        raise AssertionError(path)

    monkeypatch.setattr(hub, "_request", gateway)
    monkeypatch.setattr(hub, "hub_origin", lambda: "https://gateway.test")
    monkeypatch.setenv("LLANO_GATEWAY_API_KEY", "test-key")
    hub._identity_cache.clear()
    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    c.home, c.calls = home, calls
    return c


def _skill(home, name, body="正文\n"):
    directory = home / "skills" / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: 周报\n---\n{body}", encoding="utf-8")
    return directory


def test_my_skills_report_the_local_copy_and_unpublished_changes(client):
    from tools.skills_guard import content_hash

    directory = _skill(client.home, "weekly-report")
    receipts = client.home / "skills" / ".hub-published"
    receipts.mkdir(parents=True)
    (receipts / "weekly-report.json").write_text(json.dumps({"content_hash": content_hash(directory)}))

    rows = {row["slug"]: row for row in client.get("/api/skills/hub/mine").json()["skills"]}
    assert rows["weekly-report"]["local_name"] == "weekly-report" and not rows["weekly-report"]["local_changed"]
    assert rows["old-flow"]["local_name"] is None and rows["old-flow"]["delisted_reason"] == "流程已废止"

    (directory / "SKILL.md").write_text("---\nname: weekly-report\ndescription: 周报\n---\n后台复盘补了一步\n", encoding="utf-8")
    rows = {row["slug"]: row for row in client.get("/api/skills/hub/mine").json()["skills"]}
    assert rows["weekly-report"]["local_changed"], "本机改过、还没发布"


def test_gateway_failure_is_reported_not_swallowed(client, monkeypatch):
    from tools import llano_skill_hub as hub

    def down(path, **_):
        raise hub.SkillHubUnavailable("连不上网关")

    monkeypatch.setattr(hub, "_request", down)
    response = client.get("/api/skills/hub/mine")
    assert response.status_code == 502 and "连不上网关" in response.json()["detail"]


def test_company_cards_mark_only_the_current_accounts_skills(client):
    from hermes_cli.web_server import _skill_meta_to_payload
    from tools.llano_skill_hub import _to_meta

    mine = _skill_meta_to_payload(_to_meta({"slug": "a", "name": "甲", "owner_uid": "me"}))
    theirs = _skill_meta_to_payload(_to_meta({"slug": "b", "name": "乙", "owner_uid": "other"}))
    unknown = _skill_meta_to_payload(_to_meta({"slug": "c", "name": "丙"}))
    assert mine["mine"] and not theirs["mine"] and not unknown["mine"]
    assert client.calls.count("/v1/skill-hub/usage-identity") == 1, "当前账号只查一次"
