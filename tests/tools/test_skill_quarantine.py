"""技能隔离区。

产品前提：技能来源不封禁，安全靠扫描兜底 —— 命中即隔离，员工可自助恢复，
恢复动作要留痕。所以这里的用例分成三组：**隔离真的让技能停止生效**、
**员工看得到原因**、**恢复能放回原处并产出可上报的事件**。
"""
import json

import pytest

from tools import skill_quarantine as q


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _make_skill(home, relpath="productivity/note-taker", body="# 技能"):
    d = home / "skills" / relpath
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    (d / "scripts").mkdir(exist_ok=True)
    (d / "scripts" / "run.py").write_text("print('hi')\n", encoding="utf-8")
    return d


# ── 隔离真的让技能停止生效 ────────────────────────────────────────────────

def test_baseline_a_freshly_made_skill_is_discoverable(home):
    """基线：不隔离时它就在活动树里。

    先立这条，否则下面「隔离后找不到了」可能只是因为技能压根没建起来。
    """
    _make_skill(home)

    assert list((home / "skills").rglob("SKILL.md"))


def test_quarantine_removes_the_skill_from_the_active_tree(home):
    skill = _make_skill(home)

    q.quarantine_skill(skill, reason="扫描命中")

    assert not skill.exists()
    assert list((home / "skills").rglob("SKILL.md")) == []


def test_quarantine_lives_outside_the_active_tree(home):
    """隔离区不能在 SKILLS_DIR 底下。

    全仓 28 处 rglob("SKILL.md") 只有 21 处紧跟 is_excluded_skill_path，
    把「还生不生效」压在排除名单上就是在赌剩下那几处 —— 赌输的表现是
    技明明显示已隔离、实际还在跑。放活动树外面就没有这个依赖。
    """
    skill = _make_skill(home)
    record = q.quarantine_skill(skill, reason="扫描命中")

    payload = q.quarantine_root() / record.entry_id / "payload"

    assert payload.is_dir()
    assert (payload / "SKILL.md").is_file()
    with pytest.raises(ValueError):
        payload.resolve().relative_to((home / "skills").resolve())


def test_quarantine_moves_rather_than_copies(home):
    """留一份在活动树里就等于没隔离。"""
    skill = _make_skill(home)
    record = q.quarantine_skill(skill, reason="扫描命中")

    assert (q.quarantine_root() / record.entry_id / "payload" / "scripts" / "run.py").is_file()
    assert not (home / "skills" / "productivity" / "note-taker").exists()


def test_refuses_to_quarantine_outside_the_active_tree(home, tmp_path):
    """放不回去的东西就不该搬走。"""
    stray = tmp_path / "elsewhere" / "some-skill"
    stray.mkdir(parents=True)
    (stray / "SKILL.md").write_text("x", encoding="utf-8")

    with pytest.raises(ValueError):
        q.quarantine_skill(stray, reason="扫描命中")

    assert stray.is_dir(), "拒绝之后不能把人家目录搬走"


# ── 员工看得到原因 ────────────────────────────────────────────────────────

def test_the_employee_can_see_why_it_was_quarantined(home):
    skill = _make_skill(home)
    findings = [{"pattern_id": "exfil_http_post", "severity": "critical", "line": 12}]

    q.quarantine_skill(
        skill,
        reason="Skills Guard 判定 dangerous",
        verdict="dangerous",
        findings=findings,
        scanner_version="skills-guard-v1",
        bundle_hash="abc123",
        source="clawhub",
    )

    listed = q.list_quarantined()

    assert len(listed) == 1
    assert listed[0].reason == "Skills Guard 判定 dangerous"
    assert listed[0].findings == findings
    assert listed[0].scanner_version == "skills-guard-v1"
    assert listed[0].source == "clawhub"


def test_a_corrupt_record_does_not_hide_the_others(home):
    """一条记录坏掉不能让整个隔离区打不开 —— 员工至少要看得见其余的。"""
    q.quarantine_skill(_make_skill(home, "a/one"), reason="r1")
    q.quarantine_skill(_make_skill(home, "b/two"), reason="r2")

    broken = next(iter(q.quarantine_root().iterdir()))
    (broken / q.MANIFEST_NAME).write_text("{ 这不是 json", encoding="utf-8")

    assert len(q.list_quarantined()) == 1


def test_restored_entries_drop_out_of_the_default_listing(home):
    skill = _make_skill(home)
    record = q.quarantine_skill(skill, reason="扫描命中")
    q.restore_skill(record.entry_id)

    assert q.list_quarantined() == []
    assert len(q.list_quarantined(include_restored=True)) == 1


# ── 恢复 ──────────────────────────────────────────────────────────────────

def test_restore_puts_the_skill_back_where_it_came_from(home):
    """按原相对路径放回，不是拍到根上。

    技能是按 category/skill 分层的，放错层等于换了个技能。
    """
    skill = _make_skill(home, "productivity/note-taker")
    record = q.quarantine_skill(skill, reason="扫描命中")

    q.restore_skill(record.entry_id)

    restored = home / "skills" / "productivity" / "note-taker"
    assert (restored / "SKILL.md").is_file()
    assert (restored / "scripts" / "run.py").is_file()


def test_restore_emits_an_event_for_the_gateway(home):
    """不拦人，但越过扫描结论的动作必须留痕。"""
    skill = _make_skill(home)
    record = q.quarantine_skill(
        skill, reason="扫描命中", verdict="dangerous",
        findings=[{"pattern_id": "x"}], scanner_version="skills-guard-v1",
        bundle_hash="deadbeef", source="github",
    )

    event = q.restore_skill(record.entry_id)

    assert event["event"] == "skill_quarantine_restored"
    assert event["entry_id"] == record.entry_id
    assert event["skill_name"] == "note-taker"
    assert event["verdict"] == "dangerous"
    assert event["finding_count"] == 1
    assert event["bundle_hash"] == "deadbeef"
    assert event["source"] == "github"
    assert event["restored_at"]


def test_restore_refuses_to_clobber_a_reinstalled_skill(home):
    """隔离期间同名技能又被装了一份，恢复不能把新的那份悄悄干掉。"""
    skill = _make_skill(home, "productivity/note-taker", body="旧的")
    record = q.quarantine_skill(skill, reason="扫描命中")
    _make_skill(home, "productivity/note-taker", body="新装的")

    with pytest.raises(FileExistsError):
        q.restore_skill(record.entry_id)

    assert (home / "skills" / "productivity" / "note-taker" / "SKILL.md").read_text(encoding="utf-8") == "新装的"


def test_restoring_twice_is_refused(home):
    skill = _make_skill(home)
    record = q.quarantine_skill(skill, reason="扫描命中")
    q.restore_skill(record.entry_id)

    with pytest.raises(ValueError):
        q.restore_skill(record.entry_id)


def test_restore_of_an_unknown_entry_is_refused(home):
    with pytest.raises(FileNotFoundError):
        q.restore_skill("does-not-exist")


def test_manifest_is_written_before_the_payload_moves(home, monkeypatch):
    """搬完再写记录的话，中途崩掉会留下一份不知道该放回哪里的孤儿载荷。"""
    skill = _make_skill(home)
    monkeypatch.setattr(q.shutil, "move", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))

    with pytest.raises(OSError):
        q.quarantine_skill(skill, reason="扫描命中")

    entries = [e for e in q.quarantine_root().iterdir() if (e / q.MANIFEST_NAME).is_file()]
    assert len(entries) == 1, "崩在搬运上时，记录必须已经落盘"
    saved = json.loads((entries[0] / q.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert saved["origin_relpath"] == "productivity/note-taker"
