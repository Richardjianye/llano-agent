"""模型目录查询 —— 上游供应商 / 模型 / 白名单"""

from typing import List, Optional, Tuple

from db import init_async_session
from model.database import ModelInfo, ModelWhitelist, UpstreamProvider
from sqlalchemy import select


# ---------- 上游供应商 ----------

async def get_provider(provider_id: int) -> Optional[UpstreamProvider]:
    session_maker = await init_async_session()
    async with session_maker() as session:
        return (
            await session.execute(
                select(UpstreamProvider).where(UpstreamProvider.id == provider_id)
            )
        ).scalar_one_or_none()


async def get_provider_by_code(code: str) -> Optional[UpstreamProvider]:
    session_maker = await init_async_session()
    async with session_maker() as session:
        return (
            await session.execute(
                select(UpstreamProvider).where(UpstreamProvider.code == code)
            )
        ).scalar_one_or_none()


async def list_providers(include_disabled: bool = True) -> List[UpstreamProvider]:
    session_maker = await init_async_session()
    async with session_maker() as session:
        stmt = select(UpstreamProvider).order_by(UpstreamProvider.id)
        if not include_disabled:
            stmt = stmt.where(UpstreamProvider.status == 1)
        return list((await session.execute(stmt)).scalars().all())


# ---------- 模型 ----------

async def get_model(model_id: int) -> Optional[ModelInfo]:
    session_maker = await init_async_session()
    async with session_maker() as session:
        return (
            await session.execute(select(ModelInfo).where(ModelInfo.id == model_id))
        ).scalar_one_or_none()


async def list_models(
    provider_id: Optional[int] = None, include_disabled: bool = True
) -> List[Tuple[ModelInfo, str]]:
    """返回 (模型, 供应商编码) 列表"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        stmt = (
            select(ModelInfo, UpstreamProvider.code)
            .join(UpstreamProvider, UpstreamProvider.id == ModelInfo.provider_id)
            .order_by(ModelInfo.id)
        )
        if provider_id is not None:
            stmt = stmt.where(ModelInfo.provider_id == provider_id)
        if not include_disabled:
            stmt = stmt.where(ModelInfo.status == 1)
        return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]


async def find_model_by_ref(model_ref: str) -> Optional[ModelInfo]:
    """按对外别名或上游模型名查找"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        model = (
            await session.execute(select(ModelInfo).where(ModelInfo.alias == model_ref))
        ).scalar_one_or_none()
        if model:
            return model
        return (
            await session.execute(
                select(ModelInfo).where(ModelInfo.model_name == model_ref)
            )
        ).scalars().first()


# ---------- 白名单 ----------

async def list_whitelist(
    scope: Optional[str] = None, scope_value: Optional[str] = None
) -> List[ModelWhitelist]:
    session_maker = await init_async_session()
    async with session_maker() as session:
        stmt = select(ModelWhitelist).order_by(ModelWhitelist.id)
        if scope is not None:
            stmt = stmt.where(ModelWhitelist.scope == scope)
        if scope_value is not None:
            stmt = stmt.where(ModelWhitelist.scope_value == scope_value)
        return list((await session.execute(stmt)).scalars().all())


async def get_whitelist_entry(entry_id: int) -> Optional[ModelWhitelist]:
    session_maker = await init_async_session()
    async with session_maker() as session:
        return (
            await session.execute(
                select(ModelWhitelist).where(ModelWhitelist.id == entry_id)
            )
        ).scalar_one_or_none()


async def load_access_snapshot():
    """白名单视图需要的全量数据：部门树、成员、规则。几百行，一次读完在内存里算。"""
    from model.database import Department, User

    session_maker = await init_async_session()
    async with session_maker() as session:
        depts = list((await session.execute(select(Department))).scalars().all())
        users = list((await session.execute(select(User))).scalars().all())
        rules = list((await session.execute(select(ModelWhitelist).order_by(ModelWhitelist.id))).scalars().all())
    return depts, users, rules
