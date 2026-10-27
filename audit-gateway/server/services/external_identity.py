"""显式绑定外部企业身份；provider / tenant / subject 是跨平台唯一键。"""

from db import init_async_session
from model.database import ExternalIdentity
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError


class IdentityConflictError(Exception):
    pass


async def find_identity(provider: str, tenant_id: str, subject_id: str):
    factory = await init_async_session()
    async with factory() as session:
        return (await session.execute(select(ExternalIdentity).where(
            ExternalIdentity.provider == provider,
            ExternalIdentity.tenant_id == tenant_id,
            ExternalIdentity.subject_id == subject_id,
        ))).scalar_one_or_none()


async def find_user_identity(provider: str, tenant_id: str, user_uid: str):
    factory = await init_async_session()
    async with factory() as session:
        return (await session.execute(select(ExternalIdentity).where(
            ExternalIdentity.provider == provider,
            ExternalIdentity.tenant_id == tenant_id,
            ExternalIdentity.user_uid == user_uid,
        ))).scalar_one_or_none()


async def bind_identity(
    provider: str, tenant_id: str, subject_id: str, user_uid: str,
    provider_user_id: str | None = None,
) -> None:
    """只允许空位绑定；并发竞争由两条数据库唯一约束最终拦截。"""
    factory = await init_async_session()
    async with factory() as session:
        existing_subject = (await session.execute(select(ExternalIdentity).where(
            ExternalIdentity.provider == provider,
            ExternalIdentity.tenant_id == tenant_id,
            ExternalIdentity.subject_id == subject_id,
        ))).scalar_one_or_none()
        existing_user = (await session.execute(select(ExternalIdentity).where(
            ExternalIdentity.provider == provider,
            ExternalIdentity.tenant_id == tenant_id,
            ExternalIdentity.user_uid == user_uid,
        ))).scalar_one_or_none()
        if existing_subject or existing_user:
            if (existing_subject and existing_user
                    and existing_subject.id == existing_user.id):
                return
            raise IdentityConflictError("钉钉身份或本系统账号已绑定其他账号")

        session.add(ExternalIdentity(
            provider=provider,
            tenant_id=tenant_id,
            subject_id=subject_id,
            user_uid=user_uid,
            provider_user_id=provider_user_id,
        ))
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise IdentityConflictError("身份绑定发生冲突，请刷新后重试") from exc
