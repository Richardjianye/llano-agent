"""已装公司技能的更新（2026-10-16，P1）。

真实临时 HERMES_HOME：隔离区、安全扫描、安装、安装登记、备份全部走实际代码；
只把网关替换在 HTTP 边界（`tools.llano_skill_hub._request`），并记录每一次请求，
用来证明「检查更新不取包」「换版本取包带 purpose=update」。
"""
import json

import pytest


def _skill_md(body: str) -> str:
    return f'---\nname: weekly-report\ndisplay_name: "周报"\ndescription: "汇总一周销售数据。"\n---\n\n{body}\n'


class FakeGateway:
    def __init__(self):
        self.calls = []
        self.version = 1
        self.status = "listed"
        self.body = "第一版步骤"

    @property
    def bundle_hash(self):
        return f"h{self.version}"

    def __call__(self, path, *, method="GET", body=None):
        self.calls.append(path)
        if path == "/v1/skill-hub/metrics":
            slugs = json.loads(body)["slugs"]
            if self.status != "listed":
                return {"metrics": {s: {"status": "delisted", "delisted_reason": "流程已废止"} for s in slugs}}
            return {"metrics": {s: {
                "status": "listed", "version": self.version, "changelog": f"第{self.version}版",
                "download_count": 3, "usage_count": 5,
                "display_metadata": {"display_name": "周报", "description": "汇总一周销售数据。",
                                     "metadata_source": "llano-hub", "metadata_schema_version": 1,
                                     "bundle_hash": self.bundle_hash}} for s in slugs}}
        if path.startswith("/v1/skill-hub/skills/weekly-report/bundle"):
            return {"slug": "weekly-report", "name": "周报", "version": self.version,
                    "bundle_hash": self.bundle_hash, "metadata_source": "llano-hub",
                    "files": [{"path": "SKILL.md", "encoding": "utf-8", "content": _skill_md(self.body)}]}
        raise AssertionError(path)


@pytest.fixture
def hub(monkeypatch, _isolate_hermes_home):
    from hermes_constants import get_hermes_home
    from tools import llano_skill_hub, skills_hub
    from tools.skills_guard import scan_skill

    home = get_hermes_home()
    (home / "skills").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(skills_hub, "SKILLS_DIR", home / "skills")
    gateway = FakeGateway()
    monkeypatch.setattr(llano_skill_hub, "_request", gateway)
    monkeypatch.setattr(llano_skill_hub, "hub_origin", lambda: "https://gateway.test")

    # 首次安装：与技能中心安装同一套隔离 → 扫描 → 安装 → 登记。
    bundle = llano_skill_hub.bundle_from_payload(
        gateway("/v1/skill-hub/skills/weekly-report/bundle"), "weekly-report")
    quarantine = skills_hub.quarantine_bundle(bundle)
    skills_hub.install_from_quarantine(quarantine, "weekly-report", "productivity", bundle,
                                       scan_skill(quarantine, source=bundle.identifier))
    gateway.calls.clear()
    gateway.home = home
    gateway.skill = home / "skills" / "productivity" / "weekly-report" / "SKILL.md"
    return gateway


def _publish(gateway, version, body):
    gateway.version, gateway.body = version, body


def test_no_update_until_the_author_publishes_and_checking_never_fetches_a_bundle(hub):
    from tools.llano_skill_update import fetch_metrics, update_states

    assert update_states(fetch_metrics(["weekly-report"]))["weekly-report"]["update_available"] is False
    _publish(hub, 2, "第二版步骤")
    state = update_states(fetch_metrics(["weekly-report"]))["weekly-report"]
    assert state["update_available"] and not state["locally_modified"]
    assert state["installed_version"] == 1 and state["latest_version"] == 2 and state["changelog"] == "第2版"
    assert all("/bundle" not in call for call in hub.calls), "检查更新不取包，就不会虚增下载量"


def test_updating_an_untouched_skill_installs_the_new_version_without_counting_a_download(hub):
    from tools.llano_skill_update import update_skill
    from tools.skills_hub import HubLockFile

    _publish(hub, 2, "第二版步骤")
    result = update_skill("weekly-report")

    assert result == {"name": "weekly-report", "updated": True, "version": 2, "backup": None}
    assert "第二版步骤" in hub.skill.read_text(encoding="utf-8")
    assert hub.calls == ["/v1/skill-hub/skills/weekly-report/bundle?purpose=update"]
    entry = HubLockFile().get_installed("weekly-report")
    assert entry["metadata"]["bundle_hash"] == "h2" and entry["install_path"] == "productivity/weekly-report"


def test_local_changes_are_never_overwritten_silently_and_are_backed_up_when_confirmed(hub):
    from tools.llano_skill_update import LocalChanges, fetch_metrics, update_skill, update_states

    hub.skill.write_text(_skill_md("我自己补的一步"), encoding="utf-8")
    _publish(hub, 2, "第二版步骤")
    assert update_states(fetch_metrics(["weekly-report"]))["weekly-report"]["locally_modified"]

    with pytest.raises(LocalChanges) as refused:
        update_skill("weekly-report")
    assert str(refused.value).startswith("SKILL_LOCALLY_MODIFIED::weekly-report::")
    assert "我自己补的一步" in hub.skill.read_text(encoding="utf-8"), "没确认就什么都不动"

    result = update_skill("weekly-report", backup_local_changes=True)
    assert "第二版步骤" in hub.skill.read_text(encoding="utf-8")
    from pathlib import Path
    backup = Path(result["backup"])
    assert backup.parent == hub.home / "skills" / ".archive"
    assert "我自己补的一步" in (backup / "SKILL.md").read_text(encoding="utf-8")


def test_keeping_my_version_silences_only_that_version(hub):
    from tools.llano_skill_update import fetch_metrics, keep_local_version, update_states

    hub.skill.write_text(_skill_md("我自己补的一步"), encoding="utf-8")
    _publish(hub, 2, "第二版步骤")
    keep_local_version("weekly-report", "h2")
    assert not update_states(fetch_metrics(["weekly-report"]))["weekly-report"]["update_available"]

    _publish(hub, 3, "第三版步骤")
    assert update_states(fetch_metrics(["weekly-report"]))["weekly-report"]["update_available"], "作者再发新版照常提示"
    assert "我自己补的一步" in hub.skill.read_text(encoding="utf-8")


def test_update_all_skips_locally_changed_skills_and_reports_them(hub):
    from tools.llano_skill_update import update_all

    hub.skill.write_text(_skill_md("我自己补的一步"), encoding="utf-8")
    _publish(hub, 2, "第二版步骤")
    assert update_all() == {"updated": [], "locally_modified": ["weekly-report"], "failed": []}
    assert "我自己补的一步" in hub.skill.read_text(encoding="utf-8")
    assert all("/bundle" not in call for call in hub.calls)


def test_a_delisted_skill_is_reported_and_left_in_place(hub):
    from tools.llano_skill_update import fetch_metrics, update_states

    hub.status = "delisted"
    state = update_states(fetch_metrics(["weekly-report"]))["weekly-report"]
    assert state["delisted"] and state["delisted_reason"] == "流程已废止" and not state["update_available"]
    assert hub.skill.exists()


def test_the_cli_update_all_path_uses_the_same_rules(hub):
    from io import StringIO

    from rich.console import Console

    from hermes_cli.skills_hub import do_check, do_update

    _publish(hub, 2, "第二版步骤")
    out = StringIO()
    do_check(console=Console(file=out, width=200))
    assert "update_available" in out.getvalue()
    assert all("/bundle" not in call for call in hub.calls), "检查不取包"

    do_update(console=Console(file=out, width=200))
    assert "第二版步骤" in hub.skill.read_text(encoding="utf-8")
    assert [c for c in hub.calls if "/bundle" in c] == ["/v1/skill-hub/skills/weekly-report/bundle?purpose=update"]


def test_the_skills_list_rows_carry_the_update_state_from_the_same_metrics_batch(hub, monkeypatch):
    import hermes_cli.product_policy as policy
    from tools.skill_hub_usage_sync import local_skill_metrics

    monkeypatch.setattr(policy, "load_product_policy", lambda: object())
    _publish(hub, 2, "第二版步骤")
    row = local_skill_metrics([{"name": "weekly-report"}])[0]
    assert row["hub_update"]["update_available"] and row["hub_update"]["latest_version"] == 2
    assert row["usage_count"] == 5
    assert hub.calls == ["/v1/skill-hub/metrics"], "列表一次批量请求，不取包"


@pytest.mark.parametrize("error, status", [("local", 409), ("value", 400), ("gateway", 502)])
def test_rest_update_maps_refusals_to_statuses_the_ui_can_act_on(_isolate_hermes_home, monkeypatch, error, status):
    from starlette.testclient import TestClient

    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app
    from tools import llano_skill_update
    from tools.llano_skill_hub import SkillHubUnavailable

    def refuse(name, **_):
        raise {"local": llano_skill_update.LocalChanges(name), "value": ValueError("取不到新版本"),
               "gateway": SkillHubUnavailable("连不上网关")}[error]

    monkeypatch.setattr(llano_skill_update, "update_skill", refuse)
    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    response = client.post("/api/skills/hub/company/update", json={"name": "weekly-report"})
    assert response.status_code == status
    if error == "local":
        assert response.json()["detail"].startswith("SKILL_LOCALLY_MODIFIED::weekly-report::")


def test_the_sidebar_count_reads_metrics_only(hub):
    from tools.llano_skill_update import pending_update_count

    assert pending_update_count() == 0
    _publish(hub, 2, "第二版步骤")
    assert pending_update_count() == 1
    assert all("/bundle" not in call for call in hub.calls)
