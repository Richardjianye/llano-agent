"""技能隔离事件与样本的写入 —— 阶段 7 · S7-C

只有一个入口。调用方（router/v1/skill_reports.py）负责认证与载荷解析，
这里负责「样本去重」和「事件幂等」两件与数据库强相关的事。

## 幂等靠唯一键，不靠先查后写

先 SELECT 再 INSERT 在并发下必然漏 —— 客户端超时重试恰好是并发重传最常见的
来源，而那正是幂等键要挡的场景。这里直接写，撞唯一键就当作「已经收过了」。
"""

import json
from typing import Optional

from db import init_async_session
from model.database import SkillSecurityEvent, SkillSecuritySample, now
from services.skill_report import ParsedEvent, ParsedSample
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError


async def _upsert_sample(session, sample: ParsedSample) -> int:
    """按内容指纹去重。同一份坏技能在五十台机器上被隔离，只存一份。"""
    existing = (
        await session.execute(
            select(SkillSecuritySample.id).where(
                SkillSecuritySample.sample_hash == sample.sample_hash
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        # 计数与「最近一次」还是要更新：审查的人要看的正是「这东西还在扩散吗」。
        await session.execute(
            update(SkillSecuritySample)
            .where(SkillSecuritySample.id == existing)
            .values(
                seen_count=SkillSecuritySample.seen_count + 1,
                last_seen_at=now(),
                # 隔离时算不出 bundle_hash 的那批先落了空值，后面补上。
                bundle_hash=sample.bundle_hash or SkillSecuritySample.bundle_hash,
            )
        )
        return existing

    try:
        result = await session.execute(
            insert(SkillSecuritySample).values(
                sample_hash=sample.sample_hash,
                bundle_hash=sample.bundle_hash or None,
                file_count=sample.file_count,
                included_count=sample.included_count,
                included_bytes=sample.included_bytes,
                truncated=sample.truncated,
                truncated_reason=sample.truncated_reason or None,
                files_json=sample.files_json,
                seen_count=1,
                first_seen_at=now(),
                last_seen_at=now(),
            )
        )
        return int(result.inserted_primary_key[0])
    except IntegrityError:
        # 两台机器同时上报同一份样本。唯一键挡住了后来的那个，回查即可。
        await session.rollback()
        return int(
            (
                await session.execute(
                    select(SkillSecuritySample.id).where(
                        SkillSecuritySample.sample_hash == sample.sample_hash
                    )
                )
            ).scalar_one()
        )


async def insert_skill_event(
    *,
    parsed: ParsedEvent,
    user_uid: str,
    dept_id: Optional[int],
    dept_code: Optional[str],
) -> dict:
    """写入一条上报事件。返回 {"stored": bool, "event_id": ...}。

    身份三项来自网关对 API Key 的解析，不取载荷里的同名字段 —— 客户端跑在
    员工自己的机器上，它说自己是谁没有意义，而这张表的用途恰恰是「谁越过了
    扫描结论」。
    """
    session_maker = await init_async_session()
    async with session_maker() as session:
        sample_id = None
        if parsed.sample is not None:
            sample_id = await _upsert_sample(session, parsed.sample)

        try:
            await session.execute(
                insert(SkillSecurityEvent).values(
                    event_id=parsed.event_id,
                    event=parsed.event,
                    user_uid=user_uid,
                    dept_id=dept_id,
                    dept_code=dept_code,
                    device=parsed.device or None,
                    skill_name=parsed.skill_name,
                    entry_id=parsed.entry_id or None,
                    verdict=parsed.verdict or None,
                    reason=parsed.reason or None,
                    source=parsed.source or None,
                    origin_relpath=parsed.origin_relpath or None,
                    scanner_version=parsed.scanner_version or None,
                    bundle_hash=parsed.bundle_hash or None,
                    finding_count=parsed.finding_count,
                    findings_json=parsed.findings_json,
                    sample_id=sample_id,
                    occurred_at=parsed.occurred_at or None,
                    reported_at=parsed.reported_at or None,
                    schema_version=parsed.schema_version,
                    created_at=now(),
                )
            )
        except IntegrityError:
            # 重传。样本那边的 seen_count 已经加过一次，代价可以接受：
            # 那个数是「上报次数」，重传本来也是一次上报。
            await session.rollback()
            return {"stored": False, "event_id": parsed.event_id, "duplicate": True}

        await session.commit()
        return {"stored": True, "event_id": parsed.event_id, "duplicate": False}
