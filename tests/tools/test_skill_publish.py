"""把本地技能发布到公司 skill hub（阶段 11 · S11-F）

发布是**用户主动按下的动作**，所以这一层的规矩跟 LlanoHubSource 的读路径相反：
读路径失败要安静让路（一次网络抖动不该让搜索报错），发布失败必须说清楚为什么
—— 静默失败意味着员工以为自己交了这个月的技能指标，hub 上什么都没有。

分三组：打包、载荷组装、发布结果的措辞。
"""

from __future__ import annotations

import base64
import json

import pytest

from tools import skill_publish
from tools.llano_skill_hub import MAX_FILE_BYTES, SkillHubUnavailable
from tools.skill_publish import build_payload, pack_skill, publish_skill_action

SKILL_MD = "---\nname: 领星日报\ndescription: 拉当日订单并出汇总\n---\n\n# 步骤\n"


@pytest.fixture
def skill_dir(tmp_path):
    d = tmp_path / "lingxing-daily-report"
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (d / "scripts" / "run.py").write_text("print(1)\n", encoding="utf-8")
    return d


# ── 打包 ─────────────────────────────────────────────────────────────────

def test_text_and_binary_get_different_envelopes(skill_dir):
    """二进制走 base64。

    skill_report 那条通道**直接丢掉**二进制是对的 —— 它是取证用的。这里是分发
    通道：丢掉一个 png，装出来的技能是坏的，而且坏得很安静（装上了，运行到引用
    那张图时才发现不在）。两条通道对同一个问题给出相反的答案，因为用途相反。
    """
    blob = bytes(range(256))
    (skill_dir / "assets").mkdir()
    (skill_dir / "assets" / "logo.png").write_bytes(blob)

    files, skipped = pack_skill(skill_dir)
    by_path = {f["path"]: f for f in files}

    assert by_path["SKILL.md"]["encoding"] == "utf-8"
    assert by_path["assets/logo.png"]["encoding"] == "base64"
    assert base64.b64decode(by_path["assets/logo.png"]["content"]) == blob


def test_development_noise_is_skipped(skill_dir):
    (skill_dir / ".git").mkdir()
    (skill_dir / ".git" / "config").write_text("x", encoding="utf-8")
    (skill_dir / "__pycache__").mkdir()
    (skill_dir / "__pycache__" / "run.cpython-311.pyc").write_bytes(b"\x00\x01")
    (skill_dir / ".DS_Store").write_bytes(b"\x00")
    (skill_dir / "scripts" / "run.pyc").write_bytes(b"\x00")

    files, skipped = pack_skill(skill_dir)

    assert {f["path"] for f in files} == {"SKILL.md", "scripts/run.py"}
    assert {s["path"] for s in skipped} >= {
        ".git/config", "__pycache__/run.cpython-311.pyc", ".DS_Store", "scripts/run.pyc"
    }


def test_node_modules_is_skipped_because_it_would_blow_the_size_limit(skill_dir):
    """单独钉这条：不跳的话发布必定顶穿上限，而报出来的错是「超过 4MB」，
    跟真正的原因（技能目录里躺着一整棵依赖树）隔了一层。"""
    (skill_dir / "node_modules" / "left-pad").mkdir(parents=True)
    (skill_dir / "node_modules" / "left-pad" / "index.js").write_text("x", encoding="utf-8")

    files, skipped = pack_skill(skill_dir)

    assert all(not f["path"].startswith("node_modules/") for f in files)
    assert any(s["path"].startswith("node_modules/") for s in skipped)


def test_skipped_files_are_reported_not_swallowed(skill_dir):
    """静默跳过的话，发布者不会知道他以为发出去的那个文件其实没发。"""
    (skill_dir / "big.bin").write_bytes(b"x" * (MAX_FILE_BYTES + 1))

    files, skipped = pack_skill(skill_dir)

    entry = next(s for s in skipped if s["path"] == "big.bin")
    assert "超过单文件上限" in entry["reason"]


def test_packing_is_deterministic(skill_dir):
    """同一份内容每次收出来一样，否则「本地算的和网关算的对不上」会变成一类
    没法排查的问题。"""
    assert pack_skill(skill_dir)[0] == pack_skill(skill_dir)[0]


# ── 载荷组装 ──────────────────────────────────────────────────────────────

def test_name_and_description_come_from_the_frontmatter(skill_dir):
    payload, _ = build_payload(skill_dir, slug="lingxing-daily-report")

    assert payload["name"] == "领星日报"
    assert payload["description"] == "拉当日订单并出汇总"
    assert payload["schema_version"] == 1


def test_the_hub_name_is_the_chinese_display_name_when_the_skill_has_one(tmp_path):
    # CUSTOM（决策 #59）：hub 的 name 是展示名、slug 才是标识。带了 display_name 就发它，
    # 公司技能库里同事看到的就是中文名；安装走 slug，不受影响
    d = tmp_path / "invoice-helper"
    d.mkdir()
    (d / "SKILL.md").write_text(
        '---\nname: invoice-helper\ndisplay_name: "发票助手"\n'
        'description: "处理中文发票识别。"\n---\n\n# x\n',
        encoding="utf-8",
    )

    payload, _ = build_payload(d, slug="invoice-helper")

    assert payload["name"] == "发票助手"
    assert payload["slug"] == "invoice-helper"


def test_missing_frontmatter_is_sent_for_server_owned_metadata_completion(tmp_path):
    d = tmp_path / "bare"
    d.mkdir()
    (d / "SKILL.md").write_text("# 没有 frontmatter\n", encoding="utf-8")

    payload, _ = build_payload(d, slug="bare")
    assert payload["name"] == "bare"
    assert payload["description"] == ""
    assert payload["files"][0]["content"] == "# 没有 frontmatter\n"


def test_a_directory_without_skill_md_is_rejected_locally(tmp_path):
    """本地就拦一道。

    等网关回 400 的话，员工已经等着整个包上传完了 —— 而这个错误在打包那一刻
    就完全可知。
    """
    d = tmp_path / "notaskill"
    d.mkdir()
    (d / "readme.txt").write_text("x", encoding="utf-8")

    with pytest.raises(SkillHubUnavailable, match="SKILL.md"):
        build_payload(d, slug="notaskill")


# ── 发布结果的措辞 ────────────────────────────────────────────────────────

@pytest.fixture
def _published(monkeypatch, skill_dir):
    """把 _find_skill 和 HTTP 都换掉，留下真实的打包与措辞逻辑。"""
    import tools.skill_manager_tool as smt

    monkeypatch.setattr(smt, "_find_skill", lambda name: {"path": str(skill_dir)})

    sent = {}

    def fake_publish(payload):
        sent["payload"] = payload
        # 默认按阶段 21 之后的网关回：新技能一律落**部门**空间。
        return sent.get(
            "response",
            {"created": True, "slug": payload["slug"], "version": 1, "scope": "dept"},
        )

    monkeypatch.setattr(skill_publish, "publish_bundle", fake_publish)
    return sent


def test_publish_explains_department_and_management_visibility(_published):
    """部门范围及指定管理人员可见，不能宣称所有员工已获访问权。"""
    result = publish_skill_action("lingxing-daily-report", changelog="首版")

    assert result["success"] is True
    assert result["published"] is True
    assert "本部门及下级部门同事" in result["message"]
    assert "经理及以上、管理部门和技术部门" in result["message"]
    assert "全公司可浏览" not in result["message"]
    assert "主管" in result["message"]
    assert _published["payload"]["changelog"] == "首版"


def test_publish_result_warns_without_claiming_admin_approval(_published):
    _published["response"] = {
        "created": True, "slug": "lingxing-daily-report", "version": 1,
        "scope": "dept",
        "scan_warning": {"verdict": "dangerous", "finding_count": 9,
                         "report_status": "queued"},
    }
    result = publish_skill_action("lingxing-daily-report")
    assert result["success"] is True
    assert result["scan_warning"]["finding_count"] == 9
    assert "已在本机排队上报" in result["message"]
    assert "放行" not in result["message"]


def test_a_promoted_skill_keeps_saying_company_wide(_published):
    """晋级是**技能级**的，不是版本级的。

    主管把技能提到公司空间之后，发布者再发 v2 —— 那一版仍然在公司空间。
    这时候还说「本部门可见」，等于告诉他晋级被撤销了。
    所以措辞跟着网关返回的实际 scope 走，不是一律说部门。
    """
    _published["response"] = {
        "created": True, "slug": "lingxing-daily-report", "version": 2, "scope": "company",
    }

    result = publish_skill_action("lingxing-daily-report")

    assert "全公司可见" in result["message"]


def test_an_old_gateway_gets_no_invented_visibility(_published):
    """网关没回 scope（阶段 21 之前的版本）时**不编**范围。

    往哪边猜都会有一半的时候是错的，而说错方向的代价比少说一句大得多：
    猜「全公司」会让人去跨部门宣传一个搜不到的技能，猜「本部门」会让人跑去
    找主管晋级一个早就全公司可见的技能。少说一句只是少一句。
    """
    _published["response"] = {"created": True, "slug": "lingxing-daily-report", "version": 1}

    result = publish_skill_action("lingxing-daily-report")

    assert result["success"] is True
    assert "本部门" not in result["message"]
    assert "全公司" not in result["message"]


def test_unchanged_content_is_not_reported_as_a_successful_publish(_published):
    """「内容没变」必须说成「内容没变」。

    员工按了发布却什么都没发生，他有权知道是哪一种 —— 说成「发布成功」的话，
    他会以为这个月的技能指标交上去了。
    """
    _published["response"] = {"created": False, "slug": "lingxing-daily-report", "version": 3}

    result = publish_skill_action("lingxing-daily-report")

    assert result["success"] is True
    assert result["published"] is False
    assert "没有新增版本" in result["message"]


def test_skipped_files_reach_the_user(_published, skill_dir):
    (skill_dir / ".DS_Store").write_bytes(b"\x00")

    result = publish_skill_action("lingxing-daily-report")

    assert result["skipped"]
    assert "跳过" in result["message"]


def test_a_gateway_rejection_is_surfaced_not_swallowed(_published, monkeypatch):
    """与读路径相反：发布失败必须说清楚。

    LlanoHubSource 的 search/fetch 静默让路是对的（一次抖动不该让搜索报错），
    发布静默失败是灾难。
    """
    def boom(payload):
        raise SkillHubUnavailable("slug 只能用小写字母、数字和中划线")

    monkeypatch.setattr(skill_publish, "publish_bundle", boom)

    result = publish_skill_action("lingxing-daily-report")

    assert result["success"] is False
    assert "slug 只能用小写字母" in result["error"]


def test_publishing_a_skill_that_is_not_installed_fails_clearly(monkeypatch):
    import tools.skill_manager_tool as smt

    monkeypatch.setattr(smt, "_find_skill", lambda name: None)

    result = publish_skill_action("nope")

    assert result["success"] is False
    assert "本地没有" in result["error"]


def test_skill_manage_dispatches_the_publish_action(_published):
    """守卫写对了不等于接上了 —— 阶段 6 那次变异验证的同一个教训：
    上面所有用例测的都是 publish_skill_action，没有一条测 skill_manage 的分派。
    """
    from tools.skill_manager_tool import skill_manage

    body = json.loads(skill_manage(action="publish", name="lingxing-daily-report", content="首版"))

    assert body["success"] is True
    assert body["published"] is True


def test_publish_is_in_the_tool_schema_so_the_model_can_reach_it():
    """枚举里没有 publish 的话，模型永远不会调它 —— 而分派代码照样是「对」的。"""
    from tools.skill_manager_tool import SKILL_MANAGE_SCHEMA

    assert "publish" in SKILL_MANAGE_SCHEMA["parameters"]["properties"]["action"]["enum"]
