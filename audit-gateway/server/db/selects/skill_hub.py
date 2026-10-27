"""自有 skill hub 的读取 —— 阶段 11 · S11-C

## 搜索是 LIKE，不是全文索引

技能数量的量级是「一家公司一年做出来的」，几百到几千。这个规模下 LIKE 扫一遍
的成本远低于维护一套 FTS 的成本，也不需要中文分词（MySQL 的 ngram 全文索引对
中文技能名的效果并不比 LIKE 好多少）。

哪天真的慢了，要换的是这一个函数，接口形状不用动 —— 这是把它单独放在这里、
而不是让 router 自己拼查询的理由。

## 下架的技能对客户端**彻底不存在**

不是「标记为下架但还能装」。下架的用途就是止血：管理员按下它，是因为这个技能
不该再被装到新机器上。所以 search / inspect / bundle 三条路径一律按 status 过滤，
而不是只在列表里藏起来。
"""

import json
from typing import Any, Dict, List, Optional

from db import init_async_session
from model.database import Department, SkillHubSkill, SkillHubVersion, SkillHubUsageEvent, now
from services.org_tree import dept_ancestor_ids
from services.skill_hub import LISTED, SCOPE_COMPANY, SCOPE_DEPT
from sqlalchemy import case, func, or_, select, update


def _meta(skill: SkillHubSkill, version: Optional[SkillHubVersion] = None) -> Dict[str, Any]:
    """给客户端看的元数据。**不含 files_json** —— 那是 bundle 接口的事。

    搜索结果里带内容的话，一次「搜 report」就会把几十个技能包全传下来。
    """
    out: Dict[str, Any] = {
        "slug": skill.slug,
        "name": skill.name,
        "description": skill.description or "",
        "metadata_source": "llano-hub",
        "metadata_schema_version": 1,
        "version": int(skill.latest_version or 0),
        "owner_uid": skill.owner_uid,
        "owner_username": skill.owner_username or "",
        "owner_dept_id": int(skill.owner_dept_id) if skill.owner_dept_id else None,
        "owner_dept_code": skill.owner_dept_code or "",
        "scope": skill.scope,
        "scope_dept_id": int(skill.scope_dept_id) if skill.scope_dept_id else None,
        "install_count": int(skill.install_count or 0),
        "download_count": int(skill.install_count or 0),
        "updated_at": skill.updated_at.isoformat(timespec="seconds") if skill.updated_at else "",
    }
    if version is not None:
        out.update(
            {
                "bundle_hash": version.bundle_hash,
                "file_count": int(version.file_count or 0),
                "total_bytes": int(version.total_bytes or 0),
                "changelog": version.changelog or "",
                "publisher_username": version.publisher_username or "",
                "published_at": version.created_at.isoformat(timespec="seconds")
                if version.created_at
                else "",
            }
        )
    return out


async def _visible_to(dept_id: Optional[int], *, all_departments: bool = False):
    """可见性条件：**公司空间 ∪ 自己部门及所有上级空间**。

    这一条被 search / inspect / bundle / catalog 四处复用。抄几份的话，改可见性
    规则时漏掉一处的表现是「搜不到但装得上」或者反过来 —— 而两者都不会报错。

    ## 2026-09-03 ~ 09-23 曾经放开成全员可见

    负责人 09-03 定「发布即全员可见」（`cee04b1cae`），09-23 收回：其他部门的
    技能对本部门没用，还把自己部门的挤到后面。现在回到阶段 21 的分层——本部门
    发布的只有本部门（及下级部门，因为上级空间对下级可见）看得见，要全公司可见
    就由主管晋级到公司空间（`scope=company`）。
    """
    if all_departments:
        return SkillHubSkill.scope.in_((SCOPE_COMPANY, SCOPE_DEPT))
    visible_depts = await dept_ancestor_ids(dept_id)
    if not visible_depts:
        # 没有部门的账号只能看到公司空间。新入职还没分配部门时技能库几乎是空的，
        # 这是对的，不是 bug。
        return SkillHubSkill.scope == SCOPE_COMPANY
    return or_(
        SkillHubSkill.scope == SCOPE_COMPANY,
        (SkillHubSkill.scope == SCOPE_DEPT)
        & (SkillHubSkill.scope_dept_id.in_(visible_depts)),
    )


def _relevance_order(dept_id: Optional[int]):
    """排序：公司空间在前，其次是自己部门的，然后（上级部门空间）按更新时间。"""
    order = [
        # scope=company 排最前。用 case 而不是按字符串排：`company` < `dept`
        # 这种「碰巧字典序对了」的写法，哪天多一个 scope 值就会静默错位。
        case((SkillHubSkill.scope == SCOPE_COMPANY, 0), else_=1),
    ]
    if dept_id is not None:
        order.append(case((SkillHubSkill.scope_dept_id == int(dept_id), 0), else_=1))
    order.append(SkillHubSkill.updated_at.desc())
    return order


async def _attach_dept_names(session, metas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """给元数据补上 `scope_dept_name` —— 界面按部门分组要的是名字，不是 ID。

    一次查完再回填，而不是每行查一次：技能库列表一屏就是几十行，
    按行查会变成几十次往返，而部门表统共 63 行。

    查不到名字时**留空**让调用方自己兜底，不在这里编一个「未知部门(31)」——
    那种字符串一旦进了界面，就分不清是数据坏了还是本来就没分组。
    """
    slugs = [m["slug"] for m in metas]
    counts = (await session.execute(select(SkillHubUsageEvent.slug, func.count(SkillHubUsageEvent.id))
        .where(SkillHubUsageEvent.slug.in_(slugs)).group_by(SkillHubUsageEvent.slug))).all() if slugs else []
    by_slug = {slug: int(count) for slug, count in counts}
    for meta in metas:
        meta["usage_count"] = by_slug.get(meta["slug"], 0)
    ids = {id_ for m in metas for id_ in (m.get("scope_dept_id"), m.get("owner_dept_id")) if id_}
    if not ids:
        owner_names = await _names_by_code(session, {m.get("owner_dept_code") for m in metas if m.get("owner_dept_code")})
        for m in metas:
            m["scope_dept_name"] = ""
            m["owner_dept_name"] = owner_names.get(m.get("owner_dept_code"), "")
        return metas

    rows = (
        await session.execute(
            select(Department.id, Department.name).where(Department.id.in_(ids))
        )
    ).all()
    names = {int(r[0]): r[1] for r in rows}
    owner_codes = {m.get("owner_dept_code") for m in metas if m.get("owner_dept_code")}
    owner_names = await _names_by_code(session, owner_codes)
    for m in metas:
        m["scope_dept_name"] = names.get(m.get("scope_dept_id") or -1, "")
        m["owner_dept_name"] = names.get(m.get("owner_dept_id") or -1) or owner_names.get(m.get("owner_dept_code"), "")
    return metas


async def _names_by_code(session, codes) -> Dict[str, str]:
    if not codes:
        return {}
    rows = (await session.execute(select(Department.code, Department.name).where(Department.code.in_(codes)))).all()
    return {code: name for code, name in rows}


async def _attach_latest_versions(
    session, skills: List[SkillHubSkill], metas: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """给列表里的每个技能补上**最新版本**的发布时间与发布人（`published_at` /
    `publisher_username`），技能中心据此显示「谁、什么时候发布的」。

    不用 skill 行的 updated_at：它会被晋级、下架、恢复刷新，2026-09-24 之前还会被
    每次下载刷新（见 get_skill_bundle），不代表「这一版什么时候发的」。
    一次按 latest_version_id 批量查，不按行查。
    """
    ids = {int(s.latest_version_id) for s in skills if s.latest_version_id}
    versions: Dict[int, Any] = {}
    if ids:
        rows = (
            await session.execute(
                select(SkillHubVersion.id, SkillHubVersion.created_at, SkillHubVersion.publisher_username)
                .where(SkillHubVersion.id.in_(ids))
            )
        ).all()
        versions = {int(r[0]): r for r in rows}
    for skill, meta in zip(skills, metas):
        row = versions.get(int(skill.latest_version_id or 0))
        meta["published_at"] = row[1].isoformat(timespec="seconds") if row and row[1] else ""
        meta["publisher_username"] = (row[2] or "") if row else ""
    return metas


async def search_skills(
    query: str = "", limit: int = 20, *, dept_id: Optional[int] = None, all_departments: bool = False
) -> List[Dict[str, Any]]:
    """按关键词搜**这个人看得到的**已上架技能（见 `_visible_to`）。

    空关键词返回按相关度排序的那批（公司空间 → 本部门 → 上级部门，同级按更新时间）。
    """
    limit = max(1, min(int(limit or 20), 100))
    session_maker = await init_async_session()
    async with session_maker() as session:
        stmt = select(SkillHubSkill).where(
            SkillHubSkill.status == LISTED, await _visible_to(dept_id, all_departments=all_departments)
        )

        needle = (query or "").strip()
        if needle:
            # LIKE 的通配符要转义，否则一个 `%` 就是「全部匹配」，
            # 而 `_` 会静默匹配任意单字符 —— 搜索结果对不上但没人会报错。
            escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            stmt = stmt.where(
                or_(
                    SkillHubSkill.slug.like(pattern, escape="\\"),
                    SkillHubSkill.name.like(pattern, escape="\\"),
                    SkillHubSkill.description.like(pattern, escape="\\"),
                )
            )

        stmt = stmt.order_by(*_relevance_order(dept_id)).limit(limit)
        rows = (await session.execute(stmt)).scalars().all()
        metas = await _attach_latest_versions(session, rows, [_meta(r) for r in rows])
        return await _attach_dept_names(session, metas)


async def metrics_for_slugs(slugs, *, dept_id=None, all_departments=False):
    """客户端技能列表的批量指标，也是已装公司技能的**更新判定**来源（2026-10-16）。

    带上最新版的 version / bundle_hash / changelog，客户端拿 bundle_hash 和安装登记里的
    比较就知道有没有新版本，不必为了检查更新去取包。下架的技能只回状态和原因、不带内容：
    员工机器上已经装着它，得告诉他为什么搜不到了；可见范围仍按 `_visible_to`。
    """
    maker = await init_async_session()
    async with maker() as session:
        visible = await _visible_to(dept_id, all_departments=all_departments)
        rows = (await session.execute(select(SkillHubSkill).where(
            SkillHubSkill.slug.in_(slugs), SkillHubSkill.status == LISTED, visible))).scalars().all()
        delisted = (await session.execute(select(SkillHubSkill).where(
            SkillHubSkill.slug.in_(slugs), SkillHubSkill.status != LISTED, visible))).scalars().all()
        ids = {int(r.latest_version_id) for r in rows if r.latest_version_id}
        versions = {int(v.id): v for v in (await session.execute(
            select(SkillHubVersion).where(SkillHubVersion.id.in_(ids)))).scalars().all()} if ids else {}
        metas = [_meta(row, versions.get(int(row.latest_version_id or 0))) for row in rows]
        metas = await _attach_dept_names(session, metas)
        out = {meta["slug"]: {"status": LISTED, "download_count": meta["download_count"], "usage_count": meta["usage_count"],
                "version": meta["version"], "changelog": meta.get("changelog", ""),
                "published_at": meta.get("published_at", ""), "publisher_username": meta.get("publisher_username", ""),
                "display_metadata": {"display_name": meta["name"], "description": meta["description"],
                    "metadata_source": meta["metadata_source"], "metadata_schema_version": meta["metadata_schema_version"],
                    "bundle_hash": meta.get("bundle_hash")}}
               for meta in metas}
        for row in delisted:
            out[row.slug] = {"status": row.status, "delisted_reason": row.delisted_reason or ""}
        return out


async def department_catalog(*, viewer_dept_id=None, department_id=None, offset=0, limit=6, all_departments=False):
    """Root department tabs; descendants contribute to their root's listed total.

    计数和列表都只算**这个人看得到的**技能（`_visible_to`）。别的部门没有对他可见
    的技能时，那个部门的页签直接不出现——否则 15 个一级部门的页签里 14 个点开都是空的；
    自己所在的一级部门始终保留，哪怕暂时一个技能都没有。
    """
    from model.database import Department

    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 100))
    visible = await _visible_to(viewer_dept_id, all_departments=all_departments)
    maker = await init_async_session()
    async with maker() as session:
        departments = (await session.execute(select(Department).order_by(Department.id))).scalars().all()
        by_id = {d.id: d for d in departments}

        def root_of(dept_id):
            visited = set()
            while dept_id in by_id and dept_id not in visited:
                visited.add(dept_id)
                parent = by_id[dept_id].parent_id
                if not parent:
                    return dept_id
                dept_id = parent
            return 0  # Missing/cyclic ancestry must not lose historical skills.

        roots = {d.id: root_of(d.id) for d in departments}
        current_root = root_of(viewer_dept_id)
        groups = {d.id: {"id": d.id, "name": d.name, "code": d.code,
                  "count": 0, "is_current": d.id == current_root, "skills": []}
                  for d in departments if not d.parent_id}
        counts = (await session.execute(
            select(SkillHubSkill.owner_dept_id, func.count(SkillHubSkill.id))
            .where(SkillHubSkill.status == LISTED, visible).group_by(SkillHubSkill.owner_dept_id)
        )).all()
        for owner, count in counts:
            root = roots.get(owner, 0)
            if root not in groups:
                groups[root] = {"id": root, "name": "未分组", "code": "", "count": 0,
                                "is_current": False, "skills": []}
            groups[root]["count"] += int(count)
        result = sorted(
            (g for g in groups.values() if g["is_current"] or g["count"] > 0),
            key=lambda d: (not d["is_current"], -d["count"], d["id"]),
        )
        if department_id is not None:
            selected_root = roots.get(department_id, department_id)
            result = [d for d in result if d["id"] == selected_root]
            if result:
                members = [key for key, root in roots.items() if root == selected_root]
                condition = SkillHubSkill.owner_dept_id.in_(members)
                if selected_root == 0:
                    condition = or_(condition, SkillHubSkill.owner_dept_id.is_(None),
                                    SkillHubSkill.owner_dept_id.notin_(list(roots)))
                skills = (await session.execute(select(SkillHubSkill)
                    .where(SkillHubSkill.status == LISTED, visible, condition)
                    .order_by(SkillHubSkill.updated_at.desc(), SkillHubSkill.id.desc())
                    .offset(offset).limit(limit))).scalars().all()
                metas = await _attach_latest_versions(session, skills, [_meta(skill) for skill in skills])
                metas = await _attach_dept_names(session, metas)
                for skill, meta in zip(skills, metas):
                    meta["scope_dept_name"] = result[0]["name"]
                    meta["publisher_dept_id"] = skill.owner_dept_id
                    result[0]["skills"].append(meta)
        for group in result:
            group["has_more"] = offset + len(group["skills"]) < group["count"]
            group["next_offset"] = offset + len(group["skills"])
        return {"departments": result, "current_department_id": current_root or None}


async def get_skill_meta(slug: str, *, dept_id: Optional[int] = None, all_departments: bool = False) -> Optional[Dict[str, Any]]:
    """单个技能的元数据（含当前版本信息），不带文件内容。"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        skill = (
            await session.execute(
                select(SkillHubSkill).where(
                    SkillHubSkill.slug == slug, SkillHubSkill.status == LISTED,
                    await _visible_to(dept_id, all_departments=all_departments),
                )
            )
        ).scalar_one_or_none()
        if skill is None:
            return None

        version = None
        if skill.latest_version_id:
            version = (
                await session.execute(
                    select(SkillHubVersion).where(SkillHubVersion.id == skill.latest_version_id)
                )
            ).scalar_one_or_none()
        return (await _attach_dept_names(session, [_meta(skill, version)]))[0]


async def get_skill_bundle(slug: str, *, dept_id: Optional[int] = None, all_departments: bool = False,
                           count_download: bool = True) -> Optional[Dict[str, Any]]:
    """取技能包本体，顺便把取包次数加一。

    `count_download=False` 给「更新已装技能」用（2026-10-16）：同一个人把装过的技能
    换成新版本不是一次新下载。以前「全部更新」每检查一次就给每个已装技能取一次包，
    下载量随着点按钮的次数涨。

    ⚠️ install_count 是**取包次数**，不是安装成功数。客户端取完之后还要过扫描、
    过安装策略，任何一步都可能失败。命名成 install 是因为它就长在安装这条路上，
    但拿它当「装机量」汇报会偏高。想要真装机量得靠客户端另外上报，那是另一件事。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        skill = (
            await session.execute(
                select(SkillHubSkill).where(
                    SkillHubSkill.slug == slug, SkillHubSkill.status == LISTED,
                    await _visible_to(dept_id, all_departments=all_departments),
                )
            )
        ).scalar_one_or_none()
        if skill is None or not skill.latest_version_id:
            return None

        version = (
            await session.execute(
                select(SkillHubVersion).where(SkillHubVersion.id == skill.latest_version_id)
            )
        ).scalar_one_or_none()
        if version is None:
            # 身份行指向一个不存在的版本。数据坏了，但对调用方来说就是「没有」。
            return None

        if count_download:
            await session.execute(
                update(SkillHubSkill)
                .where(SkillHubSkill.id == skill.id)
                # 显式把 updated_at 写回原值：TimestampMixin 的 onupdate=now 会让每次下载都刷新
                # 「更新时间」，后台与客户端看到的就成了「最近一次被下载」而不是「最近一次发版」。
                .values(install_count=SkillHubSkill.install_count + 1, updated_at=SkillHubSkill.updated_at)
            )
            await session.commit()

        payload = (await _attach_dept_names(session, [_meta(skill, version)]))[0]
        # files_json 原样透传，不在这里反序列化再序列化一次 —— 那样只是把同一份
        # JSON 解开又拼回去，白花 CPU，还多一次可能出错的地方。
        payload["files_json"] = version.files_json
        return payload


async def count_skills_by_dept() -> List[Dict[str, Any]]:
    """部门技能指标：每个部门发了多少个技能、多少个版本。

    按**版本**统计而不是只数技能：把一个技能迭代五版也是五次「总结工作经验」，
    只数技能会让持续打磨的人看起来不如到处新建的人。两个数都给出去，
    让看报表的人自己决定按哪个考核。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        skills = (
            await session.execute(
                select(
                    SkillHubSkill.owner_dept_id,
                    SkillHubSkill.owner_dept_code,
                    func.count(SkillHubSkill.id),
                ).group_by(SkillHubSkill.owner_dept_id, SkillHubSkill.owner_dept_code)
            )
        ).all()
        versions = (
            await session.execute(
                select(
                    SkillHubVersion.publisher_dept_id,
                    func.count(SkillHubVersion.id),
                ).group_by(SkillHubVersion.publisher_dept_id)
            )
        ).all()

        version_by_dept = {row[0]: int(row[1]) for row in versions}
        names = dict((await session.execute(select(Department.id, Department.name))).all())
        return [
            {
                "dept_id": row[0],
                "dept_code": row[1] or "",
                "dept_name": names.get(row[0], ""),
                "skill_count": int(row[2]),
                "version_count": version_by_dept.get(row[0], 0),
            }
            for row in skills
        ]


# ==================== 管理端 ====================
#
# 管理端与客户端读的是同一批数据，但**不能复用同一个函数**：客户端那几个一律
# 按 status == LISTED 过滤，而管理端要看的恰恰是被下架的那些（下架之后员工来问
# 「我搜不到 xxx 了」，后台得答得上来）。
#
# 给 search_skills 加一个 include_delisted 开关也能做到，但那种开关最容易出的错
# 是**默认值传反**，而传反的表现是已下架技能重新出现在员工的搜索结果里 ——
# 下架等于没发生，且没有任何一处会报错。分成两个函数就没有这个默认值。


async def list_owned_skills(owner_uid: str) -> List[Dict[str, Any]]:
    """「我发布的技能」：调用者首发的全部技能，含已下架，供作者自己管理。

    只按 owner_uid 取（首发人，与部门技能指标同一口径），不经 `_visible_to`：
    作者调岗后，留在原部门空间里的技能仍然是他发布的，他得看得到、知道状态。
    下架原因也给作者本人看 —— 员工搜不到时，作者是第一个被问的人。
    调用人数按 `skill_hub_usage_events.user_uid` 去重。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(SkillHubSkill)
                .where(SkillHubSkill.owner_uid == owner_uid)
                .order_by(SkillHubSkill.updated_at.desc(), SkillHubSkill.id.desc())
                .limit(500)
            )
        ).scalars().all()
        if not rows:
            return []
        version_ids = {int(r.latest_version_id) for r in rows if r.latest_version_id}
        versions = {}
        if version_ids:
            found = (
                await session.execute(select(SkillHubVersion).where(SkillHubVersion.id.in_(version_ids)))
            ).scalars().all()
            versions = {int(v.id): v for v in found}
        slugs = [r.slug for r in rows]
        users = dict((
            await session.execute(
                select(SkillHubUsageEvent.slug, func.count(func.distinct(SkillHubUsageEvent.user_uid)))
                .where(SkillHubUsageEvent.slug.in_(slugs))
                .group_by(SkillHubUsageEvent.slug)
            )
        ).all())
        items = []
        for r in rows:
            item = _meta(r, versions.get(int(r.latest_version_id or 0)))
            item.update({
                "status": r.status,
                "delisted_reason": r.delisted_reason or "",
                "delisted_at": r.delisted_at.isoformat(timespec="seconds") if r.delisted_at else "",
                "created_at": r.created_at.isoformat(timespec="seconds") if r.created_at else "",
                "user_count": int(users.get(r.slug, 0)),
            })
            items.append(item)
        return await _attach_dept_names(session, items)


async def admin_list_skills(
    *,
    status: str = "",
    dept_id: Optional[int] = None,
    keyword: str = "",
    page: int = 1,
    page_size: int = 20,
    restrict_to_depts: Optional[tuple] = None,
) -> tuple[List[Dict[str, Any]], int]:
    """后台技能列表。含已下架，带发布者与部门。

    `restrict_to_dept` 是**数据范围**，与 `dept_id` 那个筛选条件不是一回事：
    前者由调用方按角色算出来、调用方不能绕过，后者是页面上的下拉框。

    主管拿到 skill_hub:view 之后必须有这个 —— 上一轮特意没给 manager 权限，理由
    正是「这两个权限点没有数据范围概念，给了就是全公司可见」。现在负责人要主管做
    筛选，那就得把范围真正做出来，而不是在权限名单里加个词了事。

    ⚠️ 2026-09-02 起它是**一组**部门（本部门 + 所有下级），不是一个。二级部门的
    主管看不到三级部门发的技能，等于这个功能对他不存在 —— 而公司里 8 个主管
    正好都在这种位置上。展开在 services/org_tree。
    """
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 20), 100))

    session_maker = await init_async_session()
    async with session_maker() as session:
        conditions = []
        if restrict_to_depts is not None:
            # 主管只看得到自己**及下级**部门空间里的。已经晋级到公司空间的那些
            # 他也看不到 —— 那些归管理部门管，主管要撤回的话走「撤回」。
            #
            # 空元组是「没有任何部门」，不是「不限制」。写成 in_(()) 会命中零行，
            # 这正是我们要的方向：宁可少看，不可多看。
            conditions.append(
                SkillHubSkill.scope_dept_id.in_([int(d) for d in restrict_to_depts])
            )
        if status.strip():
            conditions.append(SkillHubSkill.status == status.strip())
        if dept_id is not None:
            conditions.append(SkillHubSkill.owner_dept_id == dept_id)
        needle = (keyword or "").strip()
        if needle:
            escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            conditions.append(
                or_(
                    SkillHubSkill.slug.like(pattern, escape="\\"),
                    SkillHubSkill.name.like(pattern, escape="\\"),
                    SkillHubSkill.owner_username.like(pattern, escape="\\"),
                )
            )

        total = int(
            (
                await session.execute(
                    select(func.count(SkillHubSkill.id)).where(*conditions)
                )
            ).scalar_one()
        )

        rows = (
            await session.execute(
                select(SkillHubSkill)
                .where(*conditions)
                .order_by(SkillHubSkill.updated_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()

        items = []
        for r in rows:
            item = _meta(r)
            item.update({
                "id": int(r.id),
                "status": r.status,
                "delisted_reason": r.delisted_reason or "",
                "delisted_at": r.delisted_at.isoformat(timespec="seconds") if r.delisted_at else "",
                "created_at": r.created_at.isoformat(timespec="seconds") if r.created_at else "",
            })
            items.append(item)
        return await _attach_dept_names(session, items), total


async def list_versions(slug: str) -> List[Dict[str, Any]]:
    """一个技能的版本历史。**不含 files_json**。

    后台列表里带上几十个技能包的内容，等于把全公司的技能源码一次性推给任何
    打得开这个页面的人 —— 与 skill_security 列表不下发样本是同一条理由。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        skill = (
            await session.execute(select(SkillHubSkill).where(SkillHubSkill.slug == slug))
        ).scalar_one_or_none()
        if skill is None:
            return []

        rows = (
            await session.execute(
                select(SkillHubVersion)
                .where(SkillHubVersion.skill_id == skill.id)
                .order_by(SkillHubVersion.version.desc())
            )
        ).scalars().all()
        names = await _names_by_code(session, {v.publisher_dept_code for v in rows if v.publisher_dept_code})

        return [
            {
                "version": int(v.version),
                "bundle_hash": v.bundle_hash,
                "file_count": int(v.file_count or 0),
                "total_bytes": int(v.total_bytes or 0),
                "changelog": v.changelog or "",
                "publisher_username": v.publisher_username or "",
                "publisher_dept_code": v.publisher_dept_code or "",
                "publisher_dept_name": names.get(v.publisher_dept_code, ""),
                "created_at": v.created_at.isoformat(timespec="seconds") if v.created_at else "",
            }
            for v in rows
        ]


async def get_version_files(slug: str, version: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """某一版的**文件本体**，给后台审阅用（2026-09-03）。

    `version` 不传时取当前该装的那一版。

    ## 为什么单独一条路径，而不是让列表带上内容

    与 `list_versions` / skill_security 的列表同一条理由：一页几十个技能包全带
    内容，等于把全公司的技能源码一次性推给任何打得开这个页面的人。取内容是一个
    **单独的动作**，将来要记查阅日志时挂点也在这里。

    ## 与技能安全的样本有一点不同，要说清楚

    那边的样本是**被隔离的**代码，未必等于任何人机器上真正跑着的东西；
    这里是**已发布**的技能包本体，就是同事装下去会执行的那一份 —— 所以它可以
    也应该做完整性校验：`bundle_hash` 由服务端按「文件路径 + 各自 sha256」算，
    审阅的人看到的就是分发出去的那份，不需要相信任何客户端。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        skill = (
            await session.execute(select(SkillHubSkill).where(SkillHubSkill.slug == slug))
        ).scalar_one_or_none()
        if skill is None:
            return None

        stmt = select(SkillHubVersion).where(SkillHubVersion.skill_id == skill.id)
        if version is None:
            # 当前版本走 latest_version_id，而不是「版本号最大的那条」：
            # 前者是身份行说了算的「该装哪一版」，后者只是碰巧最大。
            # 正常情况下两者一致，不一致时该看的是前者。
            stmt = stmt.where(SkillHubVersion.id == skill.latest_version_id)
        else:
            stmt = stmt.where(SkillHubVersion.version == int(version))

        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None

        try:
            files = json.loads(row.files_json or "[]")
        except ValueError:
            # 库里那一行坏了。返回空文件表而不是抛：审阅页要能把「这一版读不出来」
            # 显示出来，那本身就是一条值得看见的信息。
            files = []

        return {
            "slug": skill.slug,
            "name": skill.name,
            "status": skill.status,
            "scope": skill.scope,
            "version": int(row.version),
            "bundle_hash": row.bundle_hash,
            "file_count": int(row.file_count or 0),
            "total_bytes": int(row.total_bytes or 0),
            "changelog": row.changelog or "",
            "publisher_username": row.publisher_username or "",
            "published_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
            "files": files,
        }


async def count_skills_by_publisher(limit: int = 50) -> List[Dict[str, Any]]:
    """按人统计：谁发了多少个技能、多少个版本。

    与按部门那个一样给两个数：只数技能会让持续打磨一个技能的人看起来不如
    到处新建的人，而「总结工作经验」显然不该这么算。
    """
    limit = max(1, min(int(limit or 50), 200))
    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(
                    SkillHubVersion.publisher_uid,
                    SkillHubVersion.publisher_username,
                    SkillHubVersion.publisher_dept_code,
                    func.count(SkillHubVersion.id),
                    func.count(func.distinct(SkillHubVersion.skill_id)),
                )
                .group_by(
                    SkillHubVersion.publisher_uid,
                    SkillHubVersion.publisher_username,
                    SkillHubVersion.publisher_dept_code,
                )
                .order_by(func.count(SkillHubVersion.id).desc())
                .limit(limit)
            )
        ).all()
        names = await _names_by_code(session, {r[2] for r in rows if r[2]})

        return [
            {
                "uid": r[0],
                "username": r[1] or "",
                "dept_code": r[2] or "",
                "dept_name": names.get(r[2], ""),
                "version_count": int(r[3]),
                "skill_count": int(r[4]),
            }
            for r in rows
        ]
