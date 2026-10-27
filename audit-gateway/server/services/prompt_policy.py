"""版本化提示词策略包。

策略包由两个互相独立但原子发布的字段组成：公司核心规则与默认人格。已发布版本
只追加不更新；回滚也是复制历史内容形成一个新版本。所有写操作借唯一草稿行的
``revision`` 做串行化与乐观锁，避免两个后台页面静默互相覆盖。

本模块的日志和审计表都只记录 hash、版本与操作者，绝不复制正文。
"""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from typing import Any

from model.database import (
    PromptPolicyAuditLog,
    PromptPolicyDraft,
    PromptPolicyVersion,
    now,
)
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from utils.errors import AppError, ConflictError, ValidationError_


MAX_COMPANY_POLICY_CHARS = 32_000
MAX_DEFAULT_PERSONA_CHARS = 8_000
MAX_POLICY_BYTES = 128_000
HISTORY_LIMIT = 100
AUDIT_HISTORY_LIMIT = 200


class PromptPolicyConflictError(ConflictError):
    """带 HTTP 409 与当前版本信息的提示词策略并发冲突。"""

    def __init__(self, message: str, *, data: dict[str, Any]):
        super().__init__(message)
        self.http_status = 409
        self.data = data


def _normalize_text(value: str, *, field: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise ValidationError_(f"{field} 必须是字符串")

    # 两个平台换行口径统一；孤立 CR 也处理，避免旧 Mac/复制内容产生不同 hash。
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if "\x00" in normalized:
        raise ValidationError_(f"{field} 不能包含 NUL 字符")
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError_(f"{field} 不是有效 UTF-8 文本") from exc
    if len(normalized) > max_chars:
        raise ValidationError_(f"{field} 超过 {max_chars} 字符")
    return normalized


def normalize_package(
    company_core_policy: str, default_persona: str
) -> tuple[str, str]:
    """规范化策略包；允许空草稿，发布时再要求两段齐全。"""
    return (
        _normalize_text(
            company_core_policy,
            field="company_core_policy",
            max_chars=MAX_COMPANY_POLICY_CHARS,
        ),
        _normalize_text(
            default_persona,
            field="default_persona",
            max_chars=MAX_DEFAULT_PERSONA_CHARS,
        ),
    )


def _validate_package_bytes(core: str, persona: str) -> None:
    if len((core + persona).encode("utf-8")) > MAX_POLICY_BYTES:
        raise ValidationError_(f"提示词策略包超过 {MAX_POLICY_BYTES} UTF-8 字节")


def canonical_package_bytes(company_core_policy: str, default_persona: str) -> bytes:
    core, persona = normalize_package(company_core_policy, default_persona)
    _validate_package_bytes(core, persona)
    return json.dumps(
        {
            "company_core_policy": core,
            "default_persona": persona,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def content_hash(company_core_policy: str, default_persona: str) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            canonical_package_bytes(company_core_policy, default_persona)
        ).hexdigest()
    )


def _iso(value) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _actor_identity(actor) -> tuple[str, str]:
    uid = str(getattr(actor, "uid", "") or "").strip()
    name = str(getattr(actor, "username", "") or uid).strip()
    if not uid:
        raise ValidationError_("操作人缺少 uid")
    return uid[:36], (name or uid)[:64]


def draft_payload(row: PromptPolicyDraft) -> dict[str, Any]:
    return {
        "company_core_policy": row.company_core_policy or "",
        "default_persona": row.default_persona or "",
        "content_hash": row.content_hash,
        "revision": int(row.revision),
        "base_version": int(row.base_version) if row.base_version is not None else None,
        "updated_at": _iso(row.updated_at) if row.updated_by_uid else None,
        "updated_by": row.updated_by_name or row.updated_by_uid,
        "updated_by_uid": row.updated_by_uid,
    }


def version_payload(row: PromptPolicyVersion) -> dict[str, Any]:
    return {
        "version": int(row.version),
        "company_core_policy": row.company_core_policy,
        "default_persona": row.default_persona,
        "content_hash": row.content_hash,
        "published_at": _iso(row.published_at),
        "published_by": row.published_by_name or row.published_by_uid,
        "published_by_uid": row.published_by_uid,
        "source_version": (
            int(row.source_version) if row.source_version is not None else None
        ),
    }


def audit_payload(row: PromptPolicyAuditLog) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "action": row.action,
        "actor_uid": row.actor_uid,
        "actor_name": row.actor_name,
        "content_hash": row.content_hash,
        "draft_revision": (
            int(row.draft_revision) if row.draft_revision is not None else None
        ),
        "version": int(row.version) if row.version is not None else None,
        "source_version": (
            int(row.source_version) if row.source_version is not None else None
        ),
        "created_at": _iso(row.created_at),
    }


async def _session_maker():
    # 运行时导入，既避免 db 初始化环，也让测试能替换会话工厂。
    from db import init_async_session

    return await init_async_session()


@asynccontextmanager
async def _sanitized_session():
    """把 SQL 错误换成不含绑定参数的异常，避免正文随异常进入日志。"""
    try:
        maker = await _session_maker()
        async with maker() as session:
            yield session
    except SQLAlchemyError:
        # SQLAlchemy 的 DBAPIError 字符串通常会附带全部绑定参数；这里绝不能把
        # exc 串进消息或日志，否则规则正文会在一次数据库故障时被完整打印。
        raise AppError(
            code=-1,
            message="提示词策略存储失败",
            http_status=500,
        ) from None


async def _draft_row(session) -> PromptPolicyDraft:
    row = (
        await session.execute(
            select(PromptPolicyDraft).where(PromptPolicyDraft.id == 1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise RuntimeError("提示词策略草稿未初始化，请先执行数据库迁移并重启网关")
    return row


async def _latest_row(session) -> PromptPolicyVersion | None:
    return (
        await session.execute(
            select(PromptPolicyVersion)
            .order_by(PromptPolicyVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_admin_state() -> dict[str, Any]:
    async with _sanitized_session() as session:
        draft = await _draft_row(session)
        published = await _latest_row(session)
        return {
            "published": version_payload(published) if published else None,
            "draft": draft_payload(draft),
        }


async def get_effective_policy() -> dict[str, Any] | None:
    async with _sanitized_session() as session:
        row = await _latest_row(session)
        return version_payload(row) if row else None


async def get_history() -> dict[str, Any]:
    async with _sanitized_session() as session:
        versions = list(
            (
                await session.execute(
                    select(PromptPolicyVersion)
                    .order_by(PromptPolicyVersion.version.desc())
                    .limit(HISTORY_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        events = list(
            (
                await session.execute(
                    select(PromptPolicyAuditLog)
                    .order_by(PromptPolicyAuditLog.id.desc())
                    .limit(AUDIT_HISTORY_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        return {
            "items": [version_payload(row) for row in versions],
            "events": [audit_payload(row) for row in events],
        }


async def _current_revision_after_rollback(session) -> int | None:
    """冲突时结束旧事务，再读一次，保证返回的不是 REPEATABLE READ 旧快照。"""
    await session.rollback()
    row = await _draft_row(session)
    return int(row.revision)


async def save_draft(
    *,
    company_core_policy: str,
    default_persona: str,
    expected_revision: int,
    actor,
) -> dict[str, Any]:
    core, persona = normalize_package(company_core_policy, default_persona)
    digest = content_hash(core, persona)
    actor_uid, actor_name = _actor_identity(actor)
    changed_at = now()

    async with _sanitized_session() as session:
        result = await session.execute(
            update(PromptPolicyDraft)
            .where(
                PromptPolicyDraft.id == 1,
                PromptPolicyDraft.revision == expected_revision,
            )
            .values(
                company_core_policy=core,
                default_persona=persona,
                content_hash=digest,
                revision=expected_revision + 1,
                updated_by_uid=actor_uid,
                updated_by_name=actor_name,
                updated_at=changed_at,
            )
        )
        if result.rowcount != 1:
            current = await _current_revision_after_rollback(session)
            raise PromptPolicyConflictError(
                "草稿已被其他人更新，请刷新后重试",
                data={"current_revision": current},
            )

        draft = await _draft_row(session)
        session.add(
            PromptPolicyAuditLog(
                action=("draft_created" if expected_revision == 0 else "draft_updated"),
                actor_uid=actor_uid,
                actor_name=actor_name,
                content_hash=digest,
                draft_revision=expected_revision + 1,
                version=None,
                source_version=draft.base_version,
                created_at=changed_at,
            )
        )
        await session.commit()
        return draft_payload(draft)


async def publish_draft(*, expected_revision: int, actor) -> dict[str, Any]:
    actor_uid, actor_name = _actor_identity(actor)
    published_at = now()
    async with _sanitized_session() as session:
        # 先用 revision CAS 抢到唯一草稿行。它既挡住旧页面，也把 max(version)+1
        # 的计算串行化；整个版本插入与审计写入在同一事务里提交。
        claimed = await session.execute(
            update(PromptPolicyDraft)
            .where(
                PromptPolicyDraft.id == 1,
                PromptPolicyDraft.revision == expected_revision,
            )
            .values(
                revision=expected_revision + 1,
                updated_by_uid=actor_uid,
                updated_by_name=actor_name,
                updated_at=published_at,
            )
        )
        if claimed.rowcount != 1:
            current = await _current_revision_after_rollback(session)
            raise PromptPolicyConflictError(
                "草稿已被其他人更新，请刷新后重试",
                data={"current_revision": current},
            )

        draft = await _draft_row(session)
        core, persona = normalize_package(
            draft.company_core_policy, draft.default_persona
        )
        if not core or not persona:
            raise ValidationError_(
                "company_core_policy 与 default_persona 两个字段都不能为空"
            )
        digest = content_hash(core, persona)
        current = await _latest_row(session)
        if current is not None and current.content_hash == digest:
            raise ValidationError_("草稿内容与当前发布版本没有变化")

        new_version = int(current.version) + 1 if current else 1
        row = PromptPolicyVersion(
            version=new_version,
            company_core_policy=core,
            default_persona=persona,
            content_hash=digest,
            source_version=None,
            published_at=published_at,
            published_by_uid=actor_uid,
            published_by_name=actor_name,
        )
        session.add(row)
        draft.base_version = new_version
        draft.content_hash = digest
        session.add(
            PromptPolicyAuditLog(
                action="published",
                actor_uid=actor_uid,
                actor_name=actor_name,
                content_hash=digest,
                draft_revision=int(draft.revision),
                version=new_version,
                source_version=None,
                created_at=published_at,
            )
        )
        await session.commit()
        return version_payload(row)


async def rollback(
    *, target_version: int, expected_current_version: int, actor
) -> dict[str, Any]:
    actor_uid, actor_name = _actor_identity(actor)
    published_at = now()
    async with _sanitized_session() as session:
        # 回滚不改草稿正文，但仍推进 revision：发布状态已经变化，旧页面必须刷新，
        # 否则它会在用户不知情时把回滚前的基线再次发布出去。
        claimed = await session.execute(
            update(PromptPolicyDraft)
            .where(PromptPolicyDraft.id == 1)
            .values(
                revision=PromptPolicyDraft.revision + 1,
                updated_by_uid=actor_uid,
                updated_by_name=actor_name,
                updated_at=published_at,
            )
        )
        if claimed.rowcount != 1:
            raise RuntimeError("提示词策略草稿未初始化，请先执行数据库迁移")

        draft = await _draft_row(session)
        current = await _latest_row(session)
        current_version = int(current.version) if current else None
        if current is None:
            await session.rollback()
            raise ValidationError_("当前还没有已发布策略，不能回滚")
        if current_version != expected_current_version:
            await session.rollback()
            raise PromptPolicyConflictError(
                "当前发布版本已变化，请刷新后重试",
                data={"current_version": current_version},
            )
        if target_version == current_version:
            raise ValidationError_("目标版本已经是当前发布版本")

        target = (
            await session.execute(
                select(PromptPolicyVersion).where(
                    PromptPolicyVersion.version == target_version
                )
            )
        ).scalar_one_or_none()
        if target is None:
            raise ValidationError_(f"历史版本 v{target_version} 不存在")
        if target.content_hash == current.content_hash:
            raise ValidationError_("目标版本内容与当前发布版本相同")

        new_version = current_version + 1
        row = PromptPolicyVersion(
            version=new_version,
            company_core_policy=target.company_core_policy,
            default_persona=target.default_persona,
            content_hash=target.content_hash,
            source_version=int(target.version),
            published_at=published_at,
            published_by_uid=actor_uid,
            published_by_name=actor_name,
        )
        session.add(row)
        session.add(
            PromptPolicyAuditLog(
                action="rolled_back",
                actor_uid=actor_uid,
                actor_name=actor_name,
                content_hash=target.content_hash,
                draft_revision=int(draft.revision),
                version=new_version,
                source_version=int(target.version),
                created_at=published_at,
            )
        )
        await session.commit()
        return version_payload(row)
