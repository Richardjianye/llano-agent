"""健康检查

/healthz  存活探针：进程活着就返回 200，不触碰依赖
/readyz   就绪探针：探活 MySQL 与 Redis，任一不通返回 503
"""

import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy import text

router = APIRouter(tags=["健康检查"])

_STARTED_AT = time.time()


@router.get("/healthz")
async def healthz():
    """存活探针"""
    return {"status": "ok", "uptime_seconds": int(time.time() - _STARTED_AT)}


@router.get("/readyz")
async def readyz():
    """就绪探针 —— 依赖不通时返回 503，供负载均衡摘流量"""
    checks = {}

    # MySQL
    try:
        from db import init_async_session

        session_maker = await init_async_session()
        async with session_maker() as session:
            await session.execute(text("SELECT 1"))
        checks["mysql"] = "ok"
    except Exception as exc:
        logger.error(f"就绪检查 MySQL 失败: {exc}")
        checks["mysql"] = f"error: {type(exc).__name__}"

    # Redis
    try:
        from utils.cache import CacheManager

        async with CacheManager.get_instance().redis_connection() as redis:
            await redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        logger.error(f"就绪检查 Redis 失败: {exc}")
        checks["redis"] = f"error: {type(exc).__name__}"

    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )
