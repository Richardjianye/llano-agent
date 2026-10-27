"""自有 skill hub 的写入 —— 阶段 11 · S11-C

只有一个入口 `publish_skill`。调用方（router/v1/skill_hub.py）负责认证与载荷解析，
这里负责三件与数据库强相关的判断：**归属**、**同内容不新增版本**、**版本号并发**。

## 同内容不新增版本

发布是个按钮，按钮会被重复点。不去重的话版本号会变成点击次数，而「v7」这种数字
是要拿去跟同事说「你装 v7 那版」的。所以按内容指纹去重：同一个技能发同样的内容，
返回已有那一版，`created=False`。

## 版本号靠唯一键，不靠先查后写

`latest_version + 1` 在并发下必然撞。`uq_skill_hub_version (skill_id, version)`
把「两个人同时算出 v3」挡成一个失败而不是库里两行 v3 —— 撞了就重试一次，
第二次读到的 latest 已经是对方写完的。
"""

from typing import Optional

from db import init_async_session
from model.database import SkillHubSkill, SkillHubVersion, now
from services.skill_hub import (DELISTED, LISTED, SCOPE_DEPT, ParsedPublish,
                                SkillHubRejected)
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

# 撞版本号唯一键之后重试几次。并发发布同一个技能在公司内部是罕见事件，
# 两次足够；再多说明不是并发而是有别的东西一直在写。
_VERSION_RETRIES = 2


def check_publish_permission(skill, slug, publisher_uid, is_admin):
    if skill is None:
        return
    if skill.owner_uid != publisher_uid and not is_admin:
        raise SkillHubRejected(f"{slug} 是 {skill.owner_username or skill.owner_uid} 发布的，只有发布者本人能发新版本；想改的话请用自己的 slug 另发一个")
    if skill.status == DELISTED:
        raise SkillHubRejected(f"{slug} 已被管理员下架，需要先恢复上架才能发新版本")


async def preflight_publish(slug, publisher_uid, is_admin):
    """Reject ownership/status failures before any auxiliary model charge.

    The publishing transaction checks again to close concurrent edits.
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        skill = (await session.execute(select(SkillHubSkill).where(SkillHubSkill.slug == slug))).scalar_one_or_none()
        check_publish_permission(skill, slug, publisher_uid, is_admin)


async def publish_status(slug, publisher_uid, is_admin):
    """「整理并发布」打开前的只读预检（2026-10-16）：这个 slug 能不能由调用者发布。

    返回 available（还没人用）/ yours（发新版本，带当前版本号）/ taken（别人的）/
    delisted（已下架）。规则与 check_publish_permission 一致，只是不抛异常 ——
    界面要据此决定是让员工继续，还是换一个标识。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        skill = (await session.execute(select(SkillHubSkill).where(SkillHubSkill.slug == slug))).scalar_one_or_none()
    if skill is None:
        return {"slug": slug, "status": "available"}
    base = {"slug": slug, "owner_username": skill.owner_username or "", "version": int(skill.latest_version or 0)}
    if skill.owner_uid != publisher_uid and not is_admin:
        return {**base, "status": "taken"}
    if skill.status == DELISTED:
        return {**base, "status": "delisted", "delisted_reason": skill.delisted_reason or ""}
    return {**base, "status": "yours"}


async def _insert_version(
    session,
    *,
    skill_id: int,
    version: int,
    parsed: ParsedPublish,
    publisher_uid: str,
    publisher_username: Optional[str],
    publisher_dept_id: Optional[int],
    publisher_dept_code: Optional[str],
) -> int:
    result = await session.execute(
        insert(SkillHubVersion).values(
            skill_id=skill_id,
            version=version,
            bundle_hash=parsed.bundle_hash,
            files_json=parsed.files_json(),
            file_count=parsed.file_count,
            total_bytes=parsed.total_bytes,
            changelog=parsed.changelog or None,
            publisher_uid=publisher_uid,
            publisher_username=publisher_username,
            publisher_dept_id=publisher_dept_id,
            publisher_dept_code=publisher_dept_code,
            created_at=now(),
        )
    )
    return int(result.inserted_primary_key[0])


async def publish_skill(
    *,
    parsed: ParsedPublish,
    publisher_uid: str,
    publisher_username: Optional[str],
    publisher_dept_id: Optional[int],
    publisher_dept_code: Optional[str],
    is_admin: bool = False,
) -> dict:
    """发布一个技能或它的新版本。

    身份五项来自网关对 API Key 的解析，不取载荷里的同名字段 —— 客户端跑在员工
    自己的机器上，它说自己是谁没有意义，而这张表要用来算部门技能指标。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        for attempt in range(_VERSION_RETRIES + 1):
            skill = (
                await session.execute(
                    select(SkillHubSkill).where(SkillHubSkill.slug == parsed.slug)
                )
            ).scalar_one_or_none()

            # ---- 全新技能 ----
            if skill is None:
                try:
                    created = await session.execute(
                        insert(SkillHubSkill).values(
                            slug=parsed.slug,
                            name=parsed.name,
                            description=parsed.description,
                            # 客户端发布**一律进部门空间**（阶段 21）。晋级到公司
                            # 空间是主管在后台做的动作，不是发布者能选的 ——
                            # 能选的话，「主管筛选」这一步就形同虚设。
                            scope=SCOPE_DEPT,
                            scope_dept_id=publisher_dept_id,
                            latest_version=0,
                            owner_uid=publisher_uid,
                            owner_username=publisher_username,
                            owner_dept_id=publisher_dept_id,
                            owner_dept_code=publisher_dept_code,
                            status=LISTED,
                            install_count=0,
                            created_at=now(),
                            updated_at=now(),
                        )
                    )
                except IntegrityError:
                    # 两个人同时首发同一个 slug。唯一键挡住后来的那个，
                    # 回到循环开头，这次会读到对方刚建的那行并走「新版本」分支
                    # —— 于是第二个人会撞上「不是拥有者」，这是对的。
                    await session.rollback()
                    continue

                skill_id = int(created.inserted_primary_key[0])
                version_id = await _insert_version(
                    session,
                    skill_id=skill_id,
                    version=1,
                    parsed=parsed,
                    publisher_uid=publisher_uid,
                    publisher_username=publisher_username,
                    publisher_dept_id=publisher_dept_id,
                    publisher_dept_code=publisher_dept_code,
                )
                await session.execute(
                    update(SkillHubSkill)
                    .where(SkillHubSkill.id == skill_id)
                    .values(latest_version_id=version_id, latest_version=1, updated_at=now())
                )
                await session.commit()
                return {
                    "created": True,
                    "slug": parsed.slug,
                    "version": 1,
                    "bundle_hash": parsed.bundle_hash,
                    "scope": SCOPE_DEPT,
                }

            # ---- 已存在：先看有没有资格发 ----
            check_publish_permission(skill, parsed.slug, publisher_uid, is_admin)

            # 在任何 commit 之前把 scope 读出来：commit 会让这一行过期，
            # 之后再取属性会触发隐式刷新 —— 异步会话里那是直接抛错。
            #
            # 为什么要带出去：发布提示语要说清楚「发到哪儿了」。这个技能可能已经
            # 被主管晋级到公司空间了，那时候再说「已发到你部门」就是假的。
            existing_scope = str(skill.scope or SCOPE_DEPT)

            # ---- 同内容不新增版本 ----
            same = (
                await session.execute(
                    select(SkillHubVersion.version).where(
                        SkillHubVersion.skill_id == skill.id,
                        SkillHubVersion.bundle_hash == parsed.bundle_hash,
                    )
                )
            ).scalar_one_or_none()
            if same is not None:
                from services.skill_hub_metadata import has_chinese
                # Same files do not create a version, but legacy display fields
                # must actually be repaired once. Never overwrite Chinese edits.
                repaired = {}
                if not has_chinese(skill.name):
                    repaired["name"] = parsed.name
                if not has_chinese(skill.description):
                    repaired["description"] = parsed.description
                if repaired and int(same) == skill.latest_version:
                    await session.execute(update(SkillHubSkill).where(SkillHubSkill.id == skill.id,
                        SkillHubSkill.name == skill.name, SkillHubSkill.description == skill.description,
                        SkillHubSkill.latest_version_id == skill.latest_version_id).values(**repaired))
                canonical = (await session.execute(select(SkillHubSkill.name, SkillHubSkill.description)
                    .where(SkillHubSkill.id == skill.id).with_for_update())).one()
                await session.commit()
                return {
                    "created": False,
                    "slug": parsed.slug,
                    "version": int(same),
                    "bundle_hash": parsed.bundle_hash,
                    "scope": existing_scope,
                    "name": canonical.name,
                    "description": canonical.description,
                    "reason": "内容与已发布的某一版完全相同，没有新增版本",
                }

            # ---- 新版本 ----
            next_version = int(skill.latest_version or 0) + 1
            try:
                version_id = await _insert_version(
                    session,
                    skill_id=int(skill.id),
                    version=next_version,
                    parsed=parsed,
                    publisher_uid=publisher_uid,
                    publisher_username=publisher_username,
                    publisher_dept_id=publisher_dept_id,
                    publisher_dept_code=publisher_dept_code,
                )
            except IntegrityError:
                # 版本号被人抢先了。重来一轮，这次读到的 latest 是对方写完的。
                await session.rollback()
                if attempt >= _VERSION_RETRIES:
                    raise SkillHubRejected("并发发布冲突，请重试")
                continue

            # 显示名与说明跟着最新一版走：技能改了用途，列表上还挂着旧描述
            # 比没有描述更误导人。
            await session.execute(
                update(SkillHubSkill)
                .where(SkillHubSkill.id == skill.id)
                .values(
                    name=parsed.name,
                    description=parsed.description,
                    latest_version_id=version_id,
                    latest_version=next_version,
                    updated_at=now(),
                )
            )
            await session.commit()
            return {
                "created": True,
                "slug": parsed.slug,
                "version": next_version,
                "bundle_hash": parsed.bundle_hash,
                "scope": existing_scope,
            }

        raise SkillHubRejected("并发发布冲突，请重试")
