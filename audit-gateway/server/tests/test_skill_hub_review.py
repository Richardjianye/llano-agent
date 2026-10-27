"""后台审阅技能包内容（2026-09-03）

在这之前，后台看得到技能的名字、发布者、版本数，唯独看不到**它到底是什么**。
要决定下不下架只能凭技能名猜，或者自己去客户端装一份 —— 而下架恰恰是那一页
唯一的止血手段。技能安全早就能读样本源码，技能库这边缺着是漏做。

这一组守两件事：**取内容是单独一条路径**（列表永远不带内容），
**默认取的是当前该装的那一版**（不是版本号最大的那条）。
"""

from __future__ import annotations

import inspect


def test_the_listing_never_carries_file_contents():
    """⚠️ 一页几十个技能包全带内容，等于把全公司的技能源码一次性推给任何打得开
    这个页面的人。与 skill_security 列表不下发样本是同一条理由。

    这条扫的是源码而不是响应：加一个字段不会让任何断言变红，但会静默地把
    几十份源码塞进每一次列表请求。
    """
    from db.selects import skill_hub as selects

    for fn in [selects.admin_list_skills, selects.list_versions]:
        # 找的是**属性访问**（`.files_json`），不是这几个字母：两个函数的文档字符串
        # 里就写着「不含 files_json」，按裸字符串扫会把说明本身当成违规。
        assert ".files_json" not in inspect.getsource(fn), f"{fn.__name__} 不该带文件内容"


def test_reading_files_is_its_own_endpoint():
    """取内容是一个**单独的动作**——将来要记查阅日志，挂点就在这一条上。

    做成「打开详情就顺手预取」的话，那条日志一上线就全是没有人真做过的查阅。
    """
    from router.admin import skill_hub as endpoint

    assert hasattr(endpoint, "skill_hub_files")
    assert "get_version_files" in inspect.getsource(endpoint.skill_hub_files)


def test_the_default_version_is_the_one_clients_install():
    """不传版本号时取 `latest_version_id`，不是「版本号最大的那条」。

    正常情况下两者一致；不一致时该看的是前者 —— 审阅的对象是**同事真正装下去
    的那一份**，不是碰巧号最大的那一份。
    """
    from db.selects import skill_hub as selects

    source = inspect.getsource(selects.get_version_files)

    assert "latest_version_id" in source
    assert "version.desc()" not in source, "不能退化成按版本号取最大"


def test_a_broken_row_shows_up_as_empty_files_not_as_an_error():
    """库里那一行坏了要能显示出来。

    抛异常的话，后台看到的是一次失败请求，而这件事本身值得看见：
    客户端取到的会是同一份坏数据。
    """
    from db.selects import skill_hub as selects

    source = inspect.getsource(selects.get_version_files)

    assert "except ValueError" in source
    assert "files = []" in source
