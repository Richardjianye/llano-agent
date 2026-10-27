"""模型目录更新与删除"""

from decimal import Decimal
from typing import Any, Optional

from db import init_async_session
from model.database import ModelInfo, ModelWhitelist, UpstreamProvider
from sqlalchemy import delete, update
from utils.secret_box import SecretPurpose, encrypt_secret

# 其余字段用 None 表示「不修改」，但单价的 None 是有含义的（未配置 ≠ 免费），
# 所以这里需要一个能和 None 区分开的哨兵，否则清空单价这个动作根本表达不出来。
UNSET: Any = object()


async def update_provider(
    provider_id: int,
    name: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    status: Optional[int] = None,
    remark: Optional[str] = None,
) -> bool:
    data = {}
    if name is not None:
        data["name"] = name
    if base_url is not None:
        data["base_url"] = base_url
    if api_key:
        data["api_key_encrypted"] = encrypt_secret(api_key, SecretPurpose.PROVIDER)
    if timeout_seconds is not None:
        data["timeout_seconds"] = timeout_seconds
    if status is not None:
        data["status"] = status
    if remark is not None:
        data["remark"] = remark

    if not data:
        return False

    session_maker = await init_async_session()
    async with session_maker() as session:
        await session.execute(
            update(UpstreamProvider)
            .where(UpstreamProvider.id == provider_id)
            .values(data)
        )
        await session.commit()
    return True


async def update_model(
    model_id: int,
    display_name: Optional[str] = None,
    alias: Optional[str] = UNSET,
    supports_stream: Optional[bool] = None,
    supports_multimodal: Optional[bool] = None,
    max_context_tokens: Optional[int] = None,
    billing_mode: Optional[str] = None,
    input_price_per_1m: Optional[Decimal] = UNSET,
    output_price_per_1m: Optional[Decimal] = UNSET,
    cached_input_price_per_1m: Optional[Decimal] = UNSET,
    price_per_call: Optional[Decimal] = UNSET,
    currency: Optional[str] = None,
    status: Optional[int] = None,
    remark: Optional[str] = None,
) -> bool:
    """修改模型。单价传 None 是「清空为未配置」，不传（UNSET）才是「不改动」"""
    data = {}
    if display_name is not None:
        data["display_name"] = display_name
    # 别名和单价一样需要哨兵：None 是「清空别名，改回用上游真实模型名」，
    # 不传（UNSET）才是「不改动」。当成 None=不改的话，管理端点「清空别名」
    # 会收到一句「更新成功」而库里纹丝不动（2026-09-11 线上就是这样）。
    if alias is not UNSET:
        data["alias"] = alias or None
    if supports_stream is not None:
        data["supports_stream"] = supports_stream
    if supports_multimodal is not None:
        data["supports_multimodal"] = supports_multimodal
    if max_context_tokens is not None:
        data["max_context_tokens"] = max_context_tokens
    if input_price_per_1m is not UNSET:
        data["input_price_per_1m"] = input_price_per_1m
    if output_price_per_1m is not UNSET:
        data["output_price_per_1m"] = output_price_per_1m
    if cached_input_price_per_1m is not UNSET:
        data["cached_input_price_per_1m"] = cached_input_price_per_1m
    if price_per_call is not UNSET:
        data["price_per_call"] = price_per_call
    if billing_mode is not None:
        data["billing_mode"] = billing_mode
    if currency is not None:
        data["currency"] = currency
    if status is not None:
        data["status"] = status
    if remark is not None:
        data["remark"] = remark

    if not data:
        return False

    session_maker = await init_async_session()
    async with session_maker() as session:
        await session.execute(
            update(ModelInfo).where(ModelInfo.id == model_id).values(data)
        )
        await session.commit()
    return True


async def update_whitelist_entry(
    entry_id: int, allowed: Optional[bool] = None, remark: Optional[str] = None
) -> bool:
    data = {}
    if allowed is not None:
        data["allowed"] = allowed
    if remark is not None:
        data["remark"] = remark

    if not data:
        return False

    session_maker = await init_async_session()
    async with session_maker() as session:
        await session.execute(
            update(ModelWhitelist).where(ModelWhitelist.id == entry_id).values(data)
        )
        await session.commit()
    return True


async def delete_whitelist_entry(entry_id: int) -> None:
    session_maker = await init_async_session()
    async with session_maker() as session:
        await session.execute(
            delete(ModelWhitelist).where(ModelWhitelist.id == entry_id)
        )
        await session.commit()


async def delete_whitelist_by_model(model_id: int) -> None:
    """删除模型时一并清理其白名单，避免悬挂授权"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        await session.execute(
            delete(ModelWhitelist).where(ModelWhitelist.model_id == model_id)
        )
        await session.commit()


async def delete_whitelist_entries(entry_ids) -> int:
    """批量删除，返回实际删掉的条数"""
    ids = sorted({int(i) for i in entry_ids})
    if not ids:
        return 0
    session_maker = await init_async_session()
    async with session_maker() as session:
        result = await session.execute(
            delete(ModelWhitelist).where(ModelWhitelist.id.in_(ids))
        )
        await session.commit()
        return int(result.rowcount or 0)
