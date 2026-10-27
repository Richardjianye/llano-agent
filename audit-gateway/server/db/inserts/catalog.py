"""模型目录写入"""

from decimal import Decimal
from typing import Optional

from db import init_async_session
from model.database import ModelInfo, ModelWhitelist, UpstreamProvider
from sqlalchemy import insert
from utils.secret_box import SecretPurpose, encrypt_secret


async def insert_provider(
    code: str,
    name: str,
    base_url: str,
    api_key: Optional[str] = None,
    timeout_seconds: int = 60,
    remark: Optional[str] = None,
) -> int:
    """新增上游供应商。api_key 以 Fernet 加密入库，绝不明文落表"""
    encrypted = (
        encrypt_secret(api_key, SecretPurpose.PROVIDER) if api_key else None
    )
    session_maker = await init_async_session()
    async with session_maker() as session:
        result = await session.execute(
            insert(UpstreamProvider).values(
                code=code,
                name=name,
                base_url=base_url,
                api_key_encrypted=encrypted,
                timeout_seconds=timeout_seconds,
                remark=remark,
                status=1,
            )
        )
        await session.commit()
        return result.inserted_primary_key[0]


async def insert_model(
    provider_id: int,
    model_name: str,
    display_name: str,
    alias: Optional[str] = None,
    supports_stream: bool = True,
    supports_multimodal: bool = False,
    max_context_tokens: Optional[int] = None,
    billing_mode: str = "token",
    input_price_per_1m: Optional[Decimal] = None,
    output_price_per_1m: Optional[Decimal] = None,
    cached_input_price_per_1m: Optional[Decimal] = None,
    price_per_call: Optional[Decimal] = None,
    currency: str = "CNY",
    remark: Optional[str] = None,
) -> int:
    """登记模型。单价留空表示未配置，报表会把这部分费用标成「无法估算」"""
    session_maker = await init_async_session()
    async with session_maker() as session:
        result = await session.execute(
            insert(ModelInfo).values(
                provider_id=provider_id,
                model_name=model_name,
                display_name=display_name,
                alias=alias,
                supports_stream=supports_stream,
                supports_multimodal=supports_multimodal,
                max_context_tokens=max_context_tokens,
                billing_mode=billing_mode,
                input_price_per_1m=input_price_per_1m,
                output_price_per_1m=output_price_per_1m,
                cached_input_price_per_1m=cached_input_price_per_1m,
                price_per_call=price_per_call,
                currency=currency or "CNY",
                remark=remark,
                status=1,
            )
        )
        await session.commit()
        return result.inserted_primary_key[0]


async def insert_whitelist_entry(
    scope: str,
    scope_value: str,
    model_id: int,
    allowed: bool = True,
    remark: Optional[str] = None,
) -> int:
    session_maker = await init_async_session()
    async with session_maker() as session:
        result = await session.execute(
            insert(ModelWhitelist).values(
                scope=scope,
                scope_value=scope_value or "",
                model_id=model_id,
                allowed=allowed,
                remark=remark,
            )
        )
        await session.commit()
        return result.inserted_primary_key[0]


async def insert_whitelist_entries(rows) -> int:
    """一个事务里批量插入；任何一条撞唯一约束整批回滚（调用方已扣掉已有条目）

    rows: [(scope, scope_value, model_id, allowed, remark), ...]
    """
    rows = list(rows)
    if not rows:
        return 0
    session_maker = await init_async_session()
    async with session_maker() as session:
        await session.execute(
            insert(ModelWhitelist),
            [
                {
                    "scope": scope,
                    "scope_value": scope_value or "",
                    "model_id": model_id,
                    "allowed": allowed,
                    "remark": remark,
                }
                for scope, scope_value, model_id, allowed, remark in rows
            ],
        )
        await session.commit()
    return len(rows)
