"""隔离事件与样本的上行链路（阶段 7 · S7-C）。

分四组：**样本收得对**、**队列不丢事件**、**发送的重试与放弃**、
**隔离/恢复两侧刻意相反的取舍**。
"""
import json

import pytest

from tools import skill_quarantine, skill_report


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def branded(monkeypatch):
    """装成品牌构建：有网关地址、有 API Key。"""
    monkeypatch.setattr(skill_report, "reporting_enabled", lambda: True)
    monkeypatch.setenv("LLANO_GATEWAY_API_KEY", "sk-llano-test")
    monkeypatch.setattr(skill_report, "upload_endpoint", lambda: "https://gw.example/v1/skill-reports")
    monkeypatch.setattr(skill_report, "_device_label", lambda: "test-box")


def _skill(home, relpath="a/one", body="# 技能\n"):
    d = home / "skills" / relpath
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    return d


# ── 样本收得对 ────────────────────────────────────────────────────────────

def test_baseline_text_files_are_included_with_content(tmp_path):
    """基线：正常的小文本文件要带内容上去。

    先立这条，否则下面那些「被排除」的用例可能只是因为采集函数什么都没收。
    """
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "SKILL.md").write_text("# hi\n", encoding="utf-8")

    sample = skill_report.collect_sample(payload)

    assert sample["included_count"] == 1
    assert sample["files"][0]["content"] == "# hi\n"
    assert sample["files"][0]["sha256"]


def test_binary_files_are_listed_but_not_uploaded(tmp_path):
    """二进制降级成元数据，而不是消失。

    审查的人看到「有个 .bin 没传上来」是有用的；看不到它存在过则不是 ——
    刻意藏进二进制里的东西最值得知道它在。
    """
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "blob.bin").write_bytes(b"\x00\x01\x02rest")

    sample = skill_report.collect_sample(payload)

    entry = sample["files"][0]
    assert entry["omitted"] == "binary"
    assert entry["included"] is False
    assert "content" not in entry
    assert entry["sha256"] and entry["size_bytes"] == 7


def test_oversized_file_is_listed_but_not_uploaded(tmp_path):
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "big.md").write_text("x" * 100, encoding="utf-8")

    sample = skill_report.collect_sample(payload, max_file_bytes=10)

    assert sample["files"][0]["omitted"] == "too_large"
    assert sample["included_count"] == 0


def test_total_budget_stops_including_but_keeps_listing(tmp_path):
    payload = tmp_path / "payload"
    payload.mkdir()
    for i in range(5):
        (payload / f"f{i}.md").write_text("y" * 30, encoding="utf-8")

    sample = skill_report.collect_sample(payload, max_total_bytes=60)

    assert sample["included_count"] == 2
    assert len(sample["files"]) == 5          # 五个都在清单里
    assert sample["truncated"] is True
    assert sample["truncated_reason"] == "total_bytes"


def test_sample_ordering_is_stable(tmp_path):
    """同一份技能收两次要一样 —— 网关侧按内容去重依赖这一点。"""
    payload = tmp_path / "payload"
    (payload / "sub").mkdir(parents=True)
    (payload / "b.md").write_text("b", encoding="utf-8")
    (payload / "a.md").write_text("a", encoding="utf-8")
    (payload / "sub" / "c.md").write_text("c", encoding="utf-8")

    first = [f["path"] for f in skill_report.collect_sample(payload)["files"]]
    second = [f["path"] for f in skill_report.collect_sample(payload)["files"]]

    assert first == second == sorted(first)


# ── 队列不丢事件 ──────────────────────────────────────────────────────────

def test_enqueue_writes_a_readable_event(home, branded):
    path = skill_report.enqueue({"event": "skill_quarantined", "skill_name": "x"})

    assert path is not None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["skill_name"] == "x"
    assert payload["event_id"] and payload["reported_at"] and payload["device"] == "test-box"


def test_background_flush_keeps_the_publishing_profile(home, monkeypatch):
    import threading
    from hermes_constants import (get_hermes_home, reset_hermes_home_override,
                                  set_hermes_home_override)

    seen = []
    done = threading.Event()

    def fake_flush():
        seen.append(get_hermes_home())
        done.set()

    monkeypatch.setattr(skill_report, "maybe_flush_reports", fake_flush)
    profile_home = home / "profiles" / "reviewed"
    token = set_hermes_home_override(profile_home)
    try:
        skill_report.flush_in_background()
    finally:
        reset_hermes_home_override(token)

    assert done.wait(3)
    assert seen == [profile_home]


def test_a_crash_mid_write_leaves_no_half_written_event_in_the_queue(home, branded, monkeypatch):
    """入队是原子的：写到一半崩掉，待发队列里不能出现半截 JSON。

    flush 把读不动的载荷判为坏事件挪进 failed/。要是 enqueue 直接往目标文件上写，
    一次写到一半的崩溃就会造出一条**永远发不出去的假记录**，而真正该记的那条
    从来没写完 —— 正是这套东西要防的事。

    这里不断言「用了 tmp + rename」（那是实现），而是模拟写一半就崩，
    看队列里剩下什么。
    """
    import pathlib as _pathlib

    real_write = _pathlib.Path.write_text

    def half_write(self, data, *args, **kwargs):
        real_write(self, data[: len(data) // 2], *args, **kwargs)
        raise OSError("磁盘满了")

    # 用独立的 context 而不是 monkeypatch.undo()：`monkeypatch` 是函数级共享的
    # 同一个对象，undo() 会把 home fixture 设的 HERMES_HOME 一并撤掉，
    # 后面的断言就跑到真实家目录上去了 —— 那是**必然通过**的空断言。
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_pathlib.Path, "write_text", half_write)
        with pytest.raises(OSError):
            skill_report.enqueue({"event": "a", "skill_name": "x"})

    assert skill_report.list_pending() == []


def test_enqueued_events_are_all_parseable(home, branded):
    skill_report.enqueue({"event": "a"})

    for path in skill_report.list_pending():
        json.loads(path.read_text(encoding="utf-8"))


def test_stock_hermes_does_not_spool_anything(home, monkeypatch):
    """原版 Hermes 没有网关，攒事件只是占磁盘。"""
    monkeypatch.setattr(skill_report, "reporting_enabled", lambda: False)

    assert skill_report.enqueue({"event": "a"}) is None
    assert skill_report.list_pending() == []


def test_queue_is_capped_and_drops_the_oldest(home, branded, monkeypatch):
    monkeypatch.setattr(skill_report, "MAX_PENDING_EVENTS", 3)
    for i in range(6):
        skill_report.enqueue({"event": "a", "reported_at": f"2026-08-26T00:00:0{i}Z"})

    pending = skill_report.list_pending()
    assert len(pending) == 3
    kept = sorted(json.loads(p.read_text(encoding="utf-8"))["reported_at"] for p in pending)
    assert kept == ["2026-08-26T00:00:03Z", "2026-08-26T00:00:04Z", "2026-08-26T00:00:05Z"]


# ── 发送 ──────────────────────────────────────────────────────────────────

def test_successful_upload_removes_the_event(home, branded):
    skill_report.enqueue({"event": "a"})
    seen = []

    def post(url, headers, body):
        seen.append((url, headers, json.loads(body.decode("utf-8"))))
        return 200

    result = skill_report.flush(post_json=post)

    assert result["sent"] == 1
    assert skill_report.list_pending() == []
    assert seen[0][0] == "https://gw.example/v1/skill-reports"
    assert seen[0][1]["Authorization"] == "Bearer sk-llano-test"


def test_network_failure_keeps_the_event_for_next_round(home, branded):
    skill_report.enqueue({"event": "a"})

    def post(url, headers, body):
        raise OSError("网关不可达")

    result = skill_report.flush(post_json=post)

    assert result["failed"] == 1
    assert len(skill_report.list_pending()) == 1


def test_events_move_to_failed_instead_of_being_deleted(home, branded, monkeypatch):
    """重试到顶挪进 failed/，不删。发不出去的记录**依然是记录**。"""
    monkeypatch.setattr(skill_report, "MAX_ATTEMPTS", 2)
    skill_report.enqueue({"event": "a"})

    def post(url, headers, body):
        raise OSError("nope")

    skill_report.flush(post_json=post)
    skill_report.flush(post_json=post)

    assert skill_report.list_pending() == []
    assert len(list(skill_report.failed_dir().iterdir())) == 1


def test_missing_credential_does_not_burn_retries(home, branded, monkeypatch):
    """还没登录不算失败 —— 否则开机几轮 tick 就把重试次数耗光，
    等员工真的登上来，事件已经进 failed/ 了。"""
    monkeypatch.delenv("LLANO_GATEWAY_API_KEY", raising=False)
    skill_report.enqueue({"event": "a"})

    def post(url, headers, body):
        raise AssertionError("没有凭据时不该发起请求")

    result = skill_report.flush(post_json=post)

    assert result["skipped"] == 1 and result["failed"] == 0
    assert len(skill_report.list_pending()) == 1


def test_a_server_error_is_not_treated_as_delivered(home, branded):
    """HTTP 500 不算送到。当成功就等于**网关一崩，这段时间的记录全部人间蒸发**，
    而客户端这边显示一切正常。"""
    skill_report.enqueue({"event": "a"})

    result = skill_report.flush(post_json=lambda url, headers, body: 500)

    assert result["sent"] == 0 and result["failed"] == 1
    assert len(skill_report.list_pending()) == 1


def test_one_corrupt_event_does_not_block_the_rest(home, branded):
    skill_report.enqueue({"event": "good"})
    (skill_report.pending_dir() / "00-corrupt.json").write_text("{ 不是 JSON", encoding="utf-8")

    sent = []

    def post(url, headers, body):
        sent.append(json.loads(body.decode("utf-8"))["event"])
        return 202

    result = skill_report.flush(post_json=post)

    assert sent == ["good"]
    assert result["gave_up"] == 1


def test_tick_entry_never_raises(home, branded, monkeypatch):
    """挂在 curator 循环和 CLI 启动上的东西不能把宿主带崩。"""
    skill_report.enqueue({"event": "a"})
    monkeypatch.setattr(skill_report, "flush", lambda **_: (_ for _ in ()).throw(RuntimeError("boom")))

    assert skill_report.maybe_flush_reports() is None


# ── 隔离 / 恢复两侧刻意相反的取舍 ────────────────────────────────────────

def test_quarantine_enqueues_the_sample(home, branded):
    skill = _skill(home, "a/one", body="# 坏技能\ncurl evil\n")

    record = skill_quarantine.quarantine_skill(skill, reason="扫描命中")

    events = [json.loads(p.read_text(encoding="utf-8")) for p in skill_report.list_pending()]
    assert [e["event"] for e in events] == ["skill_quarantined"]
    assert events[0]["entry_id"] == record.entry_id
    contents = [f.get("content") for f in events[0]["sample"]["files"]]
    assert "# 坏技能\ncurl evil\n" in contents


def test_quarantine_still_happens_when_the_queue_write_fails(home, branded, monkeypatch):
    """隔离 fail-open：入队失败照样隔离。

    反过来的话，一次磁盘写失败就把判定为危险的技能留在活动树里继续跑 ——
    拿真风险换审计完整性，方向错了。
    """
    monkeypatch.setattr(skill_report, "enqueue", lambda event: (_ for _ in ()).throw(OSError("满了")))
    skill = _skill(home, "a/one")

    record = skill_quarantine.quarantine_skill(skill, reason="扫描命中")

    assert not skill.exists()
    assert skill_quarantine.get_record(record.entry_id) is not None


def test_restore_refuses_when_the_queue_write_fails(home, branded, monkeypatch):
    """恢复 fail-closed：留不下痕迹就不放行。

    这里的记录**就是**安全动作 —— 我们不拦人越过扫描结论，代价是这个动作必须
    留下痕迹。与隔离侧刻意相反。
    """
    skill = _skill(home, "a/one")
    record = skill_quarantine.quarantine_skill(skill, reason="扫描命中")
    monkeypatch.setattr(skill_report, "enqueue", lambda event: (_ for _ in ()).throw(OSError("满了")))

    with pytest.raises(OSError):
        skill_quarantine.restore_skill(record.entry_id)

    # 技能没有被放回活动树
    assert not skill.exists()
    assert skill_quarantine.get_record(record.entry_id).restored_at is None


def test_restore_enqueues_before_moving_the_skill_back(home, branded):
    """顺序要求：入队在搬运之前。

    反过来的失败形态是最坏的那种 —— 技能已经回到活动树里跑着，网关那边什么都没有。
    """
    skill = _skill(home, "a/one")
    record = skill_quarantine.quarantine_skill(skill, reason="扫描命中")
    seen_state = {}

    real_move = skill_quarantine.shutil.move

    def spy_move(src, dst):
        seen_state["queued_before_move"] = any(
            json.loads(p.read_text(encoding="utf-8"))["event"] == "skill_quarantine_restored"
            for p in skill_report.list_pending()
        )
        return real_move(src, dst)

    skill_quarantine.shutil.move = spy_move
    try:
        skill_quarantine.restore_skill(record.entry_id)
    finally:
        skill_quarantine.shutil.move = real_move

    assert seen_state["queued_before_move"] is True


def test_restore_event_carries_no_sample(home, branded):
    """恢复事件不带样本 —— 同一个 bundle_hash 隔离时已经传过，网关靠它串起来。"""
    skill = _skill(home, "a/one")
    record = skill_quarantine.quarantine_skill(skill, reason="扫描命中", bundle_hash="abc123")
    for p in skill_report.list_pending():
        p.unlink()

    skill_quarantine.restore_skill(record.entry_id)

    event = json.loads(skill_report.list_pending()[0].read_text(encoding="utf-8"))
    assert event["event"] == "skill_quarantine_restored"
    assert event["bundle_hash"] == "abc123"
    assert "sample" not in event


# ── 接线 ──────────────────────────────────────────────────────────────────

def _stripped(path):
    """读源码前先剥注释 —— 断言命中自己写的说明是本会话踩过两次的坑。"""
    from pathlib import Path

    return "\n".join(
        line for line in Path(path).read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    )


@pytest.mark.parametrize("host", ["cli.py", "gateway/run.py"])
def test_the_flush_is_actually_wired_into_the_tick_sites(host):
    """队列写对了不等于有人发。

    没有 flush 的话，这套东西的失败形态是**最安静的那种**：隔离照常、员工看得见
    隔离区、本地队列一直在长，而网关那边一条都没有 —— 而网关那边有没有，
    正是「样本上传供审查」的全部内容。
    """
    source = _stripped(host)

    assert "from tools.skill_report import maybe_flush_reports" in source
    assert "maybe_flush_reports()" in source


@pytest.mark.parametrize("host", ["cli.py", "gateway/run.py"])
def test_the_flush_tick_call_is_wrapped_so_it_cannot_kill_the_host(host):
    """maybe_flush_reports 自己不抛，但 import 失败会 —— 冻结产物漏收模块
    是本阶段实测过的真实故障。"""
    source = _stripped(host)
    idx = source.index("from tools.skill_report import maybe_flush_reports")
    before = source[:idx]
    after = source[idx:idx + 400]

    assert before.rstrip().endswith("try:"), "import 必须在 try 里"
    assert "except Exception" in after


def test_skill_report_is_in_the_sidecar_module_manifest():
    """冻结产物的出厂自检要认得这个模块。

    `tools.skill_report` 只在函数体里被 import（tick 点、隔离/恢复路径），
    正是 PyInstaller 静态分析看不见的形状 —— 少收了不报错，只是上报从此静默失效。
    """
    from pathlib import Path

    manifest = Path("apps/desktop/build-sidecar/required-modules.txt").read_text(encoding="utf-8")
    assert "tools.skill_report" in manifest.split()
