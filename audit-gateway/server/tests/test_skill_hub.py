"""自有 skill hub 的发布与分发（阶段 11 · S11-B/C）

hub 存在的理由是给各部门下技能指标 —— 每月做多少 skill、总结多少工作经验，
把员工的工作经验转成公司资产。所以这一层要保证的核心是两条：

1. **发出去的东西装得上、装的是发布者发的那份**（编码往返、路径安全、内容指纹）
2. **算谁的不能由客户端说了算**（归属一律从 API Key 解，载荷里带了也忽略）

⚠️ 这里**没有**数据库级的用例。

  publish_skill 里那几条判断（不是拥有者 / 已下架 / 同内容不新增版本 / 版本号
  撞唯一键后重试）全都压在真实的唯一约束上，喂一个假 session 测出来的绿是假的
  —— 假 session 不会真的撞 uq_skill_hub_version。这一段由
  scripts/e2e/skill_hub_chain.py 对着一次性 MySQL 容器验，
  与 skill_report_chain.py 同一套办法。

分四组：编码与往返、路径安全、载荷校验、接口行为。
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from router.v1 import router as v1_router
from services.skill_hub import (MAX_FILE_BYTES, MAX_FILES, MAX_PAYLOAD_BYTES,
                                MAX_TOTAL_BYTES, SkillHubRejected,
                                compute_bundle_hash, normalize_bundle_path,
                                normalize_slug, parse_publish)
from utils.gateway_auth import Caller, require_caller
from utils.middleware import register_exception_handlers

SKILL_MD = "---\nname: demo\n---\n\n# Demo\n"


def _payload(**overrides):
    payload = {
        "schema_version": 1,
        "slug": "lingxing-daily-report",
        "name": "领星日报",
        "description": "拉当日订单并出汇总",
        "changelog": "首版",
        "files": [{"path": "SKILL.md", "encoding": "utf-8", "content": SKILL_MD}],
    }
    payload.update(overrides)
    return payload


def _parse(**overrides):
    payload = _payload(**overrides)
    return parse_publish(payload, raw_bytes=len(json.dumps(payload).encode("utf-8")))


# ── 基线 ──────────────────────────────────────────────────────────────────

def test_baseline_a_well_formed_publish_parses():
    """先立基线，否则下面所有「被拒」的用例可能只是因为解析函数什么都不接受。"""
    parsed = _parse()

    assert parsed.slug == "lingxing-daily-report"
    assert parsed.name == "领星日报"
    assert parsed.file_count == 1
    assert parsed.total_bytes == len(SKILL_MD.encode("utf-8"))
    assert len(parsed.bundle_hash) == 64


# ── 编码与往返 ────────────────────────────────────────────────────────────

def test_binary_files_survive_the_round_trip():
    """二进制必须原样回来。

    tools/skill_report.py 的样本收集是**直接丢掉**二进制的 —— 那条通道是取证用的，
    审查的人要读代码。但 hub 是**分发**通道：丢掉一个 png，装出来的技能是坏的，
    而且坏得很安静（装上了，运行到引用那张图时才发现不在）。
    """
    blob = bytes(range(256))
    parsed = _parse(
        files=[
            {"path": "SKILL.md", "encoding": "utf-8", "content": SKILL_MD},
            {"path": "assets/logo.png", "encoding": "base64",
             "content": base64.b64encode(blob).decode("ascii")},
        ]
    )

    stored = json.loads(parsed.files_json())
    png = next(f for f in stored if f["path"] == "assets/logo.png")

    assert base64.b64decode(png["content"]) == blob


def test_the_same_content_gets_the_same_hash_whichever_encoding_is_used():
    """指纹算的是**解码后**的字节。

    否则同一份内容用 utf-8 发一次、用 base64 发一次就是两个指纹，
    「同内容不新增版本」那条去重会被一个换发法的操作绕过去，
    版本号重新变成按钮点击次数。
    """
    as_text = _parse(files=[{"path": "SKILL.md", "encoding": "utf-8", "content": SKILL_MD}])
    as_b64 = _parse(files=[{
        "path": "SKILL.md", "encoding": "base64",
        "content": base64.b64encode(SKILL_MD.encode("utf-8")).decode("ascii"),
    }])

    assert as_text.bundle_hash == as_b64.bundle_hash


def test_file_order_does_not_change_the_hash():
    """发布者按什么顺序列文件是随意的，指纹不能跟着变。"""
    files = [
        {"path": "SKILL.md", "encoding": "utf-8", "content": SKILL_MD},
        {"path": "scripts/run.py", "encoding": "utf-8", "content": "print(1)\n"},
    ]

    assert _parse(files=files).bundle_hash == _parse(files=list(reversed(files))).bundle_hash


def test_changing_one_byte_changes_the_hash():
    """反向基线：指纹要是对内容不敏感，上面两条「相等」就毫无意义。"""
    a = _parse()
    b = _parse(files=[{"path": "SKILL.md", "encoding": "utf-8", "content": SKILL_MD + " "}])

    assert a.bundle_hash != b.bundle_hash


def test_moving_content_between_files_changes_the_hash():
    """指纹把路径也算进去。

    只哈希内容的话，把 SKILL.md 的内容挪到 scripts/run.py 得到同一个指纹 ——
    而那是完全不同的一个技能（一个是说明，一个是会被执行的脚本）。
    """
    a = _parse(files=[
        {"path": "SKILL.md", "encoding": "utf-8", "content": SKILL_MD},
        {"path": "scripts/a.py", "encoding": "utf-8", "content": "X"},
    ])
    b = _parse(files=[
        {"path": "SKILL.md", "encoding": "utf-8", "content": SKILL_MD},
        {"path": "scripts/b.py", "encoding": "utf-8", "content": "X"},
    ])

    assert a.bundle_hash != b.bundle_hash


def test_sloppy_base64_is_rejected_rather_than_silently_repaired():
    """base64 解码必须 validate=True。

    默认的宽松模式**静默丢掉**非 base64 字符，于是一份被改动过的载荷能解出
    一份看似正常、内容却与发布者手上那份不同的文件 —— 而这份东西会被执行。
    """
    with pytest.raises(SkillHubRejected, match="base64"):
        _parse(files=[
            {"path": "SKILL.md", "encoding": "utf-8", "content": SKILL_MD},
            {"path": "assets/x.bin", "encoding": "base64", "content": "!!!not base64!!!"},
        ])


def test_unknown_encodings_are_rejected():
    with pytest.raises(SkillHubRejected, match="encoding"):
        _parse(files=[{"path": "SKILL.md", "encoding": "rot13", "content": SKILL_MD}])


# ── 路径安全 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "bad",
    [
        "../outside.md",
        "a/../../outside.md",
        "/etc/passwd",
        "a/./../../x",
        # 反斜杠必须先归一：不归一的话 PurePosixPath 把整串当成**一个文件名**，
        # `..` 检查直接漏过去。
        r"..\..\outside.md",
        r"a\..\..\outside.md",
    ],
)
def test_traversal_paths_are_rejected(bad):
    """网关是扇出点。

    这一层拦下的东西客户端安装时还会再拦一次，但一个坏路径从这里发出去要在
    几十台机器上各拦一次，而其中任何一台的客户端版本落后一点就漏了。
    """
    with pytest.raises(SkillHubRejected):
        normalize_bundle_path(bad)


@pytest.mark.parametrize("bad", ["run.py:payload", "C:/x.md", "a/b:c/d.md"])
def test_colons_are_rejected_because_of_ntfs_alternate_data_streams(bad):
    """冒号在 Windows 上要么是盘符，要么是 NTFS 的 Alternate Data Stream。

    一个叫 `run.py:payload` 的成员会把字节写进**看不见的流**里：目录里只看得到
    `run.py`，rglob 式的复核根本枚举不到那段内容。规则抄自上游
    tools/skills_hub._normalize_bundle_path，这里必须不比它宽松。
    """
    with pytest.raises(SkillHubRejected, match="冒号"):
        normalize_bundle_path(bad)


def test_ordinary_nested_paths_still_pass():
    """反向基线：上面那些拒绝，不能是因为这个函数拒绝一切。"""
    assert normalize_bundle_path("scripts/run.py") == "scripts/run.py"
    assert normalize_bundle_path("./references/a/b.md") == "references/a/b.md"
    assert normalize_bundle_path(r"assets\logo.png") == "assets/logo.png"


def test_duplicate_paths_are_rejected():
    """同名两份内容：审查的人读到前一份，装到磁盘上的是后一份。

    这也是 files 用数组而不是 {path: content} 字典的理由 —— 字典会静默留下
    最后一个，连撞都撞不出来。
    """
    with pytest.raises(SkillHubRejected, match="重复"):
        _parse(files=[
            {"path": "SKILL.md", "encoding": "utf-8", "content": SKILL_MD},
            {"path": "SKILL.md", "encoding": "utf-8", "content": "# 另一份\n"},
        ])


# ── slug ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "bad", ["", "-lead", "trail-", "Has Space", "中文名", "a" * 65, "a/b", "a.b", "_x"]
)
def test_bad_slugs_are_rejected(bad):
    with pytest.raises(SkillHubRejected):
        normalize_slug(bad)


def test_slug_case_is_folded_but_nothing_else_is_repaired():
    """只折叠大小写和首尾空白。

    容错到「把 `My Skill` 自动改成 `my-skill`」听上去友好，实际是把发布者以为的
    名字和真正落盘的名字悄悄岔开：他回头在 hub 上搜自己刚发的技能会搜不到。
    """
    assert normalize_slug("  Lingxing-Report  ") == "lingxing-report"
    with pytest.raises(SkillHubRejected):
        normalize_slug("My Skill")


# ── 载荷校验 ──────────────────────────────────────────────────────────────

def test_a_bundle_without_skill_md_is_rejected():
    """SKILL.md 是技能的定义本身，没有它装下去是个空壳。"""
    with pytest.raises(SkillHubRejected, match="SKILL.md"):
        _parse(files=[{"path": "scripts/run.py", "encoding": "utf-8", "content": "print(1)"}])


def test_skill_md_is_matched_case_insensitively():
    """Windows 上发布的包很容易是 Skill.md。为这个拒掉一次发布毫无价值。"""
    parsed = _parse(files=[{"path": "Skill.md", "encoding": "utf-8", "content": SKILL_MD}])

    assert parsed.file_count == 1


def test_an_oversized_payload_is_rejected_rather_than_truncated():
    """超限要拒，不能截断。

    截断之后库里那一行看上去是好的，分发出去的却是残包 —— 客户端解 JSON 直接
    失败，员工看到的是「装不上」，离真正的原因隔着好几层。
    """
    with pytest.raises(SkillHubRejected, match="超过上限"):
        parse_publish(_payload(), raw_bytes=MAX_PAYLOAD_BYTES + 1)


def test_too_many_files_is_rejected():
    files = [{"path": "SKILL.md", "encoding": "utf-8", "content": SKILL_MD}]
    files += [
        {"path": f"scripts/f{i}.py", "encoding": "utf-8", "content": "x"}
        for i in range(MAX_FILES)
    ]

    with pytest.raises(SkillHubRejected, match="文件数"):
        _parse(files=files)


def test_a_single_oversized_file_is_rejected():
    with pytest.raises(SkillHubRejected, match="单文件"):
        _parse(files=[
            {"path": "SKILL.md", "encoding": "utf-8", "content": SKILL_MD},
            {"path": "big.txt", "encoding": "utf-8", "content": "x" * (MAX_FILE_BYTES + 1)},
        ])


def test_the_total_budget_is_enforced_even_when_every_file_is_small():
    """逐文件都合规、加起来超限 —— 这是只查单文件上限会漏掉的那种。"""
    chunk = "x" * (MAX_FILE_BYTES - 1)
    files = [{"path": "SKILL.md", "encoding": "utf-8", "content": SKILL_MD}]
    files += [
        {"path": f"scripts/f{i}.txt", "encoding": "utf-8", "content": chunk}
        for i in range(MAX_TOTAL_BYTES // MAX_FILE_BYTES + 1)
    ]

    with pytest.raises(SkillHubRejected, match="上限"):
        _parse(files=files)


def test_an_unknown_schema_version_is_rejected():
    """认不出来的版本直接拒。收下一个看不懂的载荷等于往库里放一行含义不明的东西。"""
    with pytest.raises(SkillHubRejected, match="schema_version"):
        _parse(schema_version=2)


def test_overlong_metadata_is_rejected_without_silent_truncation():
    with pytest.raises(SkillHubRejected, match="最多 1024"):
        _parse(description="很长" * 2000)


@pytest.mark.parametrize("field,value", [("name", "demo"), ("name", ""),
                                        ("description", "English only"), ("description", "")])
def test_publish_requires_chinese_metadata(field, value):
    with pytest.raises(SkillHubRejected, match="中文"):
        _parse(**{field: value})


def test_chinese_metadata_may_include_english_technical_names():
    assert _parse(name="Git 项目助手", description="创建 Git 仓库和提交").name == "Git 项目助手"


# ── 接口行为 ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_publish_preflight(monkeypatch):
    async def allowed(*args, **kwargs):
        return None
    monkeypatch.setattr("db.inserts.skill_hub.preflight_publish", allowed)


def _app(*, authenticated: bool = True, role: str = "employee") -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(v1_router)

    if authenticated:
        app.dependency_overrides[require_caller] = lambda: Caller(
            uid="real-uid",
            username="员工",
            role=role,
            dept_id=7,
            dept_code="rd",
            auth_type="apikey",
            api_key_id=3,
        )
    return app


def test_publishing_requires_authentication():
    """归属是这两张表的一半用途。匿名发布等于往部门指标里放没有主语的行。"""
    response = TestClient(_app(authenticated=False)).post(
        "/v1/skill-hub/skills", json=_payload()
    )

    assert response.status_code == 401


def test_identity_comes_from_the_api_key_not_the_payload(monkeypatch):
    """载荷里塞别人的 uid 一概忽略。

    否则任意一把有效 Key 都能把技能记到同事名下 —— 而部门技能指标就是从这些行
    算出来的，等于让考核数据可以被任意一台客户端随手改。
    """
    import router.v1.skill_hub as module

    seen = {}

    async def fake_publish(**kwargs):
        seen.update(kwargs)
        return {"created": True, "slug": kwargs["parsed"].slug, "version": 1, "bundle_hash": "h"}

    monkeypatch.setattr(module, "publish_skill", fake_publish)

    TestClient(_app()).post(
        "/v1/skill-hub/skills",
        json=_payload(owner_uid="someone-else", publisher_uid="someone-else", dept_id=99),
    )

    assert seen["publisher_uid"] == "real-uid"
    assert seen["publisher_dept_id"] == 7
    assert seen["publisher_dept_code"] == "rd"


def test_ordinary_employees_are_not_admins(monkeypatch):
    """全体员工都能发布，但「越过别人的所有权」是另一回事。"""
    import router.v1.skill_hub as module

    seen = {}

    async def fake_publish(**kwargs):
        seen.update(kwargs)
        return {"created": True, "slug": "x", "version": 1, "bundle_hash": "h"}

    monkeypatch.setattr(module, "publish_skill", fake_publish)

    TestClient(_app(role="employee")).post("/v1/skill-hub/skills", json=_payload())
    assert seen["is_admin"] is False

    TestClient(_app(role="developer")).post("/v1/skill-hub/skills", json=_payload())
    assert seen["is_admin"] is True


def test_a_rejected_payload_never_reaches_the_database(monkeypatch):
    import router.v1.skill_hub as module

    calls = []

    async def fake_publish(**kwargs):
        calls.append(kwargs)
        return {}

    monkeypatch.setattr(module, "publish_skill", fake_publish)

    response = TestClient(_app()).post("/v1/skill-hub/skills", json=_payload(slug="Bad Slug"))

    assert response.status_code == 400
    assert calls == []


def test_rejections_are_real_4xx_because_the_client_branches_on_2xx(monkeypatch):
    """调用方是机器。

    全站业务异常默认是 200 带 {code, message}（给后台页面读的），但客户端按
    `2xx == 成功` 分支。拒绝要是回 200，一次「slug 不合法」会被当成发布成功 ——
    员工那边一切正常，hub 上什么都没有。
    """
    for bad in [
        _payload(slug=""),
        _payload(files=[]),
        _payload(schema_version=99),
        _payload(files=[{"path": "../x.md", "encoding": "utf-8", "content": SKILL_MD}]),
    ]:
        response = TestClient(_app()).post("/v1/skill-hub/skills", json=bad)
        assert response.status_code == 400, bad


def test_bad_json_is_a_client_error_not_a_500():
    response = TestClient(_app()).post(
        "/v1/skill-hub/skills",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400


def test_publishing_identical_content_is_200_not_201(monkeypatch):
    """201 = 真的新增了一版；200 = 内容没变。

    这个区分是给人看的：员工按了发布却什么都没发生，CLI 必须说清楚是
    「已经是最新的」而不是「发出去了」。
    """
    import router.v1.skill_hub as module

    async def fake_publish(**kwargs):
        return {"created": False, "slug": "x", "version": 3, "bundle_hash": "h",
                "reason": "内容与已发布的某一版完全相同，没有新增版本"}

    monkeypatch.setattr(module, "publish_skill", fake_publish)

    response = TestClient(_app()).post("/v1/skill-hub/skills", json=_payload())

    assert response.status_code == 200
    assert response.json()["created"] is False


def test_publishing_someone_elses_skill_is_403_not_400(monkeypatch):
    """载荷没问题，是这个人此刻没资格。客户端据此提示的话术完全不同。"""
    import router.v1.skill_hub as module

    async def fake_publish(**kwargs):
        raise SkillHubRejected("x 是别人发布的")

    monkeypatch.setattr(module, "publish_skill", fake_publish)

    response = TestClient(_app()).post("/v1/skill-hub/skills", json=_payload())

    assert response.status_code == 403


def test_a_missing_skill_is_404_on_both_read_paths(monkeypatch):
    """已下架和从不存在都回 404，不区分。

    区分开等于告诉调用方「有这么个技能，只是你看不到」，而下架的用途就是
    让它对客户端彻底不存在。
    """
    import router.v1.skill_hub as module

    async def none_meta(slug, **kwargs):
        return None

    monkeypatch.setattr(module, "get_skill_meta", none_meta)
    monkeypatch.setattr(module, "get_skill_bundle", none_meta)

    client = TestClient(_app())
    assert client.get("/v1/skill-hub/skills/nope").status_code == 404
    assert client.get("/v1/skill-hub/skills/nope/bundle").status_code == 404


def test_the_bundle_endpoint_hands_back_a_parsed_array_not_a_json_string(monkeypatch):
    """wire protocol 里不留「值是 JSON 的字符串」字段。

    直接把 files_json 透传能省一次解析+序列化，但客户端就得二次 json.loads。
    取包是低频动作，不值得为这点 CPU 换一个别扭的协议。
    """
    import router.v1.skill_hub as module

    async def fake_bundle(slug, **kwargs):
        return {
            "slug": slug,
            "version": 2,
            "files_json": json.dumps(
                [{"path": "SKILL.md", "encoding": "utf-8", "content": SKILL_MD}],
                ensure_ascii=False,
            ),
        }

    monkeypatch.setattr(module, "get_skill_bundle", fake_bundle)

    body = TestClient(_app()).get("/v1/skill-hub/skills/demo/bundle").json()

    assert "files_json" not in body
    assert body["files"] == [{"path": "SKILL.md", "encoding": "utf-8", "content": SKILL_MD}]


def test_update_fetches_ask_the_store_not_to_count_a_download(monkeypatch):
    import router.v1.skill_hub as module

    seen = []

    async def fake_bundle(slug, **kwargs):
        seen.append(kwargs["count_download"])
        return {"slug": slug, "version": 1, "files_json": "[]"}

    monkeypatch.setattr(module, "get_skill_bundle", fake_bundle)
    client = TestClient(_app())
    assert client.get("/v1/skill-hub/skills/demo/bundle").status_code == 200
    assert client.get("/v1/skill-hub/skills/demo/bundle?purpose=update").status_code == 200
    assert seen == [True, False]


def test_publish_preflight_reads_status_for_the_authenticated_caller(monkeypatch):
    import db.inserts.skill_hub as inserts

    seen = []

    async def fake_status(slug, uid, is_admin):
        seen.append((slug, uid, is_admin))
        return {"slug": slug, "status": "available"}

    monkeypatch.setattr(inserts, "publish_status", fake_status)
    body = TestClient(_app()).get("/v1/skill-hub/preflight/Weekly-Report").json()
    assert body == {"slug": "weekly-report", "status": "available"}
    assert seen == [("weekly-report", "real-uid", False)]


def test_a_corrupt_row_is_not_served_as_a_broken_bundle(monkeypatch):
    """库里那行坏了就直说。

    给客户端一个解不开的包，它会在安装到一半的时候炸，比这里直接报错难查得多。
    """
    import router.v1.skill_hub as module

    async def fake_bundle(slug, **kwargs):
        return {"slug": slug, "version": 1, "files_json": "{不是 JSON"}

    monkeypatch.setattr(module, "get_skill_bundle", fake_bundle)

    assert TestClient(_app()).get("/v1/skill-hub/skills/demo/bundle").status_code == 500


def test_search_results_never_carry_file_contents(monkeypatch):
    """一次「搜 report」不能把几十个技能包全传下来。"""
    import router.v1.skill_hub as module

    async def fake_search(q, limit, **kwargs):
        return [{"slug": "a", "name": "A", "description": "", "version": 1}]

    monkeypatch.setattr(module, "search_skills", fake_search)

    body = TestClient(_app()).get("/v1/skill-hub/skills?q=report").json()

    assert body["skills"] and all("files" not in s for s in body["skills"])
