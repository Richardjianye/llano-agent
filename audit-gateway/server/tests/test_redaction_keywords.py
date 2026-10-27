"""按部门设置的屏蔽词（阶段 18）

负责人 2026-08-31 定：全局三个词（示例公司科技 / 示例公司 / llano），并要求能按部门
另配，形状参考『限流与配额』。

这一组守五件事：**相加不是替换**、**长词优先**、**按发言人部门不按读者**、
**库挂了要降级到基线而不是关掉防线**、**部门按 ID 匹配且子部门继承上级**。

最后一件是 2026-09-03 补的：规则的 scope_value 被填成部门 code，于是三条规则
对谁都不生效，而全公司仍被遮 —— 真正生效的是环境变量 MASK_KEYWORDS 那份全局
基线。两个方向的错叠在一起，看起来却像「屏蔽词没生效范围反而更大了」。
"""

from __future__ import annotations

import pytest
from services.redaction_policy import build_redaction_policy, normalized_keywords
from services import redaction_policy as rp
from services.project import redact


# ── 归一化与排序 ──────────────────────────────────────────────────────────

def test_longer_keywords_sort_first():
    """「示例公司科技」必须排在「示例公司」前面。

    短词先匹配会把长词切成「[公司]科技」—— 漏掉的正是更具体的那半截。
    这条排序是 redact 里正则「或」的顺序来源，不是装饰。
    """
    ordered = normalized_keywords("示例公司,示例公司科技,llano")

    assert ordered.index("示例公司科技") < ordered.index("示例公司")


def test_duplicates_are_folded_case_insensitively():
    """同一个词大小写不同不该占两个位置 —— 匹配本来就是不敏感的。"""
    assert len(normalized_keywords("llano,LLANO,Llano")) == 1


def test_blank_entries_are_dropped():
    """配置里多打一个逗号不该产生一个空词 —— 空词会匹配到处都是。"""
    assert normalized_keywords("a,,  ,b") == normalized_keywords("a,b")


# ── 相加不是替换 ──────────────────────────────────────────────────────────


class _FakeSession:
    """只回答 keywords_for 那一条 select。"""

    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, _stmt):
        rows = self._rows

        class _Result:
            @staticmethod
            def scalars():
                class _S:
                    @staticmethod
                    def all():
                        return rows
                return _S()

        return _Result()


def _fake_db(monkeypatch, rows, session=None):
    async def maker():
        def factory():
            return session or _FakeSession(rows)
        return factory

    monkeypatch.setattr("db.init_async_session", maker)
    # keywords_for 还会问一次部门树。这里默认「没有上级」，让不关心继承的用例
    # 保持原样；关心继承的用例用 _fake_tree 覆盖掉。
    _fake_tree(monkeypatch, lambda dept_id: (int(dept_id),))


def _fake_tree(monkeypatch, resolve):
    """替掉 org_tree.dept_ancestor_ids —— 部门树自己那套脏数据在它自己的用例里测。"""
    async def dept_ancestor_ids(dept_id):
        return resolve(dept_id)

    monkeypatch.setattr("services.org_tree.dept_ancestor_ids", dept_ancestor_ids)


def _fake_env(monkeypatch, keywords):
    """只改 mask_keywords 这一个属性。

    整个替换 get_settings 会连 jwt_secret_key 之类一起换掉，而 db 的 import 链
    会去读它们 —— 那样测出来的失败跟被测的东西无关（踩过一次）。
    """
    from config import get_settings

    monkeypatch.setattr(get_settings(), "mask_keywords", keywords, raising=False)


async def test_the_table_adds_to_the_env_baseline_it_does_not_replace_it(monkeypatch):
    """⚠️ 这条是本组最重要的。

    反过来实现（表里有行就忽略环境变量）会造成一个很难查的失败：某天有人建了
    第一条 dept 规则，全局那几个词**当场失效**，而没有任何一处会报错。
    """
    _fake_env(monkeypatch, "示例公司,llano")
    _fake_db(monkeypatch, ["部门专有词"])

    words = await rp.keywords_for(7)

    assert set(words) == {"示例公司", "llano", "部门专有词"}, \
        "环境变量那份必须还在 —— 表是相加不是替换"


async def test_an_empty_table_still_yields_the_baseline(monkeypatch):
    """反向：表是空的时候不能变成什么都不遮。

    新建的环境表里一行都没有，那时全靠基线兜着。
    （反过来 2026-09-03 之后线上是基线为空、全靠表——两头都得成立。）
    """
    _fake_env(monkeypatch, "示例公司,llano")
    _fake_db(monkeypatch, [])

    assert set(await rp.keywords_for(None)) == {"示例公司", "llano"}


async def test_a_broken_database_degrades_to_the_baseline_not_to_nothing(monkeypatch):
    """库挂了要让防线**降级到基线**，不是整个关掉。

    遮多了是噪音，遮漏了是泄漏。反过来实现的话，只要把库打挂就能让关键词防线
    彻底消失 —— 而那正是攻击者最想要的。
    """
    _fake_env(monkeypatch, "示例公司,llano")

    async def boom():
        raise RuntimeError("库挂了")

    monkeypatch.setattr("db.init_async_session", boom)

    assert set(await rp.keywords_for(7)) == {"示例公司", "llano"}, "库挂了不该丢掉基线词"


# ── 按部门解析：ID 不是 code，且子部门继承上级 ────────────────────────────


class _CapturingSession(_FakeSession):
    """把 where 里真正下发的绑定值抓出来 —— 这一层错了不会报错，只会静默不遮。"""

    def __init__(self):
        super().__init__([])
        self.params: dict = {}

    async def execute(self, stmt):
        self.params = stmt.compile().params
        return await super().execute(stmt)

    @property
    def bound_values(self) -> set[str]:
        """所有绑定值，`in_()` 那个列表拆开 —— 它是一个参数、一串值。"""
        out: set[str] = set()
        for value in self.params.values():
            if isinstance(value, (list, tuple)):
                out.update(str(v) for v in value)
            else:
                out.add(str(value))
        return out


async def test_dept_rules_are_matched_by_id_not_by_department_code(monkeypatch):
    """部门规则必须按数据库 ID 匹配，不能按部门 code 匹配。"""
    _fake_env(monkeypatch, "")
    session = _CapturingSession()
    _fake_db(monkeypatch, [], session=session)

    await rp.keywords_for(11)

    values = session.bound_values
    assert "11" in values, "下发的必须是部门 ID"
    assert "finance" not in values, "部门 code 不该出现在查询条件里"


async def test_a_child_department_inherits_the_words_of_its_parents(monkeypatch):
    """财务中心（11）配了词，核算组（31）的人也要被遮。

    子部门必须继承上级配置的规则（org_tree.expand_ancestors）。
    """
    _fake_env(monkeypatch, "")
    session = _CapturingSession()
    _fake_db(monkeypatch, [], session=session)
    _fake_tree(monkeypatch, lambda _dept: (11, 31))

    await rp.keywords_for(31)

    assert {"11", "31"} <= session.bound_values, "子部门必须连上级的规则一起查"


async def test_a_department_without_a_tree_still_matches_itself(monkeypatch):
    """部门树读不出来（脏数据/环）时退回只认本部门。

    这是**降级**不是关闭：少遮上级的词，但本部门那几个还在。
    """
    _fake_env(monkeypatch, "")
    session = _CapturingSession()
    _fake_db(monkeypatch, [], session=session)
    _fake_tree(monkeypatch, lambda _dept: ())

    await rp.keywords_for(31)

    assert "31" in session.bound_values


# ── 遮蔽行为 ──────────────────────────────────────────────────────────────

KEYWORDS = normalized_keywords("示例公司科技,示例公司,llano")


def test_the_confirmed_keywords_actually_mask():
    """负责人定的这三个词。基线用例 —— 没有它，下面的边界用例都没有意义。"""
    for text in ["示例公司的报表", "LLANO 的数据", "示例公司科技有限公司"]:
        assert redact(text, keywords=KEYWORDS, placeholder="[公司]").hits >= 1, text


def test_the_long_name_is_not_split_into_two():
    result = redact("示例公司科技有限公司", keywords=KEYWORDS, placeholder="[公司]")

    assert result.text == "[公司]有限公司"
    assert result.hits == 1, "长词被切成两半的话 hits 会是 2"


def test_unrelated_text_is_untouched():
    """反向基线：上面那些「被遮了」不能是因为它把什么都遮。"""
    assert redact("今天的订单量比昨天高", keywords=KEYWORDS, placeholder="[公司]").hits == 0


# ── 按发言人部门，不按读者 ────────────────────────────────────────────────

def test_group_chat_resolves_by_the_sender_department():
    """一条消息从 A 部门发出、被 B 部门读到，要防的是「A 的内部术语流到 B」。

    按读者遮是错的：那样 A 的术语在 B 眼里原样呈现 —— 恰恰是要防的那个方向。
    这条扫的是调用点，因为参数传错了不会有任何报错。
    """
    import inspect

    from router.v1 import projects

    source = inspect.getsource(projects.say)

    assert "redact_for_dept(body, caller.dept_id)" in source, "必须按发言人部门解析"


def test_the_policy_version_changes_with_the_keyword_set():
    """两个部门拿到同一个 version 却是不同词表的话，换个人登录就会用错词表。

    客户端按 version 缓存，所以 version 必须随词表变。
    """
    a = build_redaction_policy(keywords=["示例公司"])
    b = build_redaction_policy(keywords=["示例公司", "llano"])

    assert a["version"] != b["version"]


def test_the_same_keyword_set_keeps_a_stable_version():
    """反向：词表没变时 version 不能变，否则客户端每次都重新拉，缓存等于没有。"""
    a = build_redaction_policy(keywords=["示例公司", "llano"])
    b = build_redaction_policy(keywords=["示例公司", "llano"])

    assert a["version"] == b["version"]


def test_the_department_is_taken_from_the_credential_not_the_request():
    """否则谁都能报一个词表最短的部门来少遮几个词。"""
    import inspect

    from router.v1 import redaction_policy as endpoint

    source = inspect.getsource(endpoint.redaction_policy)

    assert "caller.dept_id" in source
    assert "Query" not in source and "Body" not in source, "部门不能由请求方指定"
