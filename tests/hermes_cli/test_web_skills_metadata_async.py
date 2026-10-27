"""Real temporary-home reads stay responsive; AI completion starts only from the button."""
import time


def test_listing_never_calls_ai_and_the_button_completes_without_blocking(tmp_path, monkeypatch, _isolate_hermes_home):
    from starlette.testclient import TestClient
    import hermes_cli.product_policy as policy
    from hermes_constants import get_hermes_home
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
    from tools import skill_chinese_metadata as chinese

    home = get_hermes_home()
    skill = home / "skills" / "metadata-route-example"
    skill.mkdir(parents=True)
    original = "---\nname: metadata-route-example\ndescription: Produce a daily report\n---\n\nRead the source and summarize it.\n"
    (skill / "SKILL.md").write_text(original)
    (home / "config.yaml").write_text("{}\n")
    monkeypatch.setattr(policy, "load_product_policy", lambda: object())
    import threading
    from tools import local_skill_metadata as local
    entered, release = threading.Event(), threading.Event()
    calls = []
    def translate(rows):
        calls.append(rows)
        entered.set()
        assert release.wait(5)
        return {r["id"]: {"name": "每日业务报表", "description": "整理来源资料并汇总每日业务数据"} for r in rows}
    monkeypatch.setattr(chinese, "_translate", translate)
    with TestClient(app) as client:
        client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
        for _ in range(3):
            row = next(r for r in client.get("/api/skills").json() if r["name"] == "metadata-route-example")
            assert row["metadata_incomplete"] and row["metadata_completable"] and not row["metadata_pending"]
            assert row["display_name"] == "metadata-route-example", "未补全时显示原文"
            assert row["description"] == "Produce a daily report"
        assert calls == [], "只是浏览列表不调用 AI"

        start = time.monotonic()
        queued = client.post("/api/skills/metadata/complete")
        assert time.monotonic() - start < 2 and queued.json() == {"queued": 1}
        row = next(r for r in client.get("/api/skills").json() if r["name"] == "metadata-route-example")
        assert row["metadata_pending"], "补全进行中，界面据此轮询"
        assert entered.wait(5)
        release.set()
        deadline = time.monotonic() + 5
        while not local._cache_path(home).exists():
            assert time.monotonic() < deadline
            time.sleep(.01)
        row = next(r for r in client.get("/api/skills").json() if r["name"] == "metadata-route-example")
        assert row["display_name"] == "每日业务报表" and not row["metadata_incomplete"]
    assert len(calls) == 1
    assert (skill / "SKILL.md").read_text() == original
