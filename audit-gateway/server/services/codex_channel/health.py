"""Codex 渠道掉号检测

「掉号」= refresh_token 被撤销 / 账号被封 / 授权过期，请求会突然全体失败。
检测有三条腿：

1. **流量侧被动标记**：转发链路一旦遇到凭证类失败（invalid_grant、上游 401），
   立刻把该渠道记成 dead —— 员工侧的报错和后台看到的状态同源。
2. **巡检主动发现**：每隔一段时间对每条 Codex 渠道做一次「续期 + 额度查询」，
   空闲渠道掉了也能被发现，不用等第一条失败流量。
3. **管理端随时查**：status 接口直接读这里的健康快照。

状态是进程内的（alive / dead / limited / unknown + 原因 + 时间），不落库：
它描述的是「此刻的连通情况」，落库反而会把瞬时状态变成历史包袱。
重启后由巡检在第一个周期内重新建立。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

from loguru import logger

STATE_ALIVE = "alive"
STATE_DEAD = "dead"
STATE_LIMITED = "limited"
STATE_UNKNOWN = "unknown"

# provider_id → {"state", "reason", "checked_at"}
_health: Dict[int, Dict[str, Any]] = {}
_task: Optional[asyncio.Task] = None


def mark(provider_id: int, state: str, reason: str = "") -> Optional[str]:
    """记录健康状态。返回状态变化描述（无变化返回 None），调用方据此打日志"""
    prev = _health.get(provider_id)
    entry = {"state": state, "reason": reason, "checked_at": time.time()}
    _health[provider_id] = entry
    if prev is None or prev.get("state") == state:
        return None
    return f"{prev.get('state')} → {state}" + (f"（{reason}）" if reason else "")


def mark_dead(provider_id: int, reason: str) -> None:
    change = mark(provider_id, STATE_DEAD, reason)
    if change:
        logger.warning(f"Codex 渠道掉号检测: provider={provider_id} {change}")


def mark_alive(provider_id: int) -> None:
    change = mark(provider_id, STATE_ALIVE)
    if change:
        logger.info(f"Codex 渠道恢复: provider={provider_id} {change}")


def snapshot(provider_id: int) -> Optional[Dict[str, Any]]:
    entry = _health.get(provider_id)
    return dict(entry) if entry else None


def _classify(exc: Exception) -> str:
    from services.codex_channel import CodexUpstreamError, OAuthLoginRequired

    if isinstance(exc, OAuthLoginRequired):
        return STATE_DEAD
    if isinstance(exc, CodexUpstreamError):
        if exc.code in ("codex_auth_failed", "codex_login_required"):
            return STATE_DEAD
        return STATE_UNKNOWN
    return STATE_UNKNOWN


async def check_channel(provider_id: int, api_key_encrypted: Optional[str], base_url: str,
                        timeout_seconds: int = 60) -> Dict[str, Any]:
    """对一条 Codex 渠道做「续期 + 额度查询」，返回健康快照

    一次 wham 查询同时回答两个问题：凭证是否活着（掉号检测）、额度用了多少
    （剩余额度）。额度查询失败但凭证活着时，状态记 limited/unknown 的区分交给
    调用方拿到的 usage 是否存在。
    """
    from services.codex_channel import CodexUpstreamError, ensure_access_token, fetch_usage
    from services.codex_channel.credentials import CredentialError

    try:
        _, cred = await ensure_access_token(provider_id, api_key_encrypted)
    except Exception as exc:
        state = STATE_DEAD if isinstance(exc, CredentialError) else _classify(exc)
        change = mark(provider_id, state, str(exc))
        if change:
            logger.warning(f"Codex 渠道掉号检测: provider={provider_id} {change}")
        return snapshot(provider_id) or {}

    try:
        usage = await fetch_usage(cred, base_url, timeout_seconds)
    except Exception as exc:  # noqa: BLE001 — 分类后转成状态
        state = _classify(exc)
        change = mark(provider_id, state, str(exc))
        if change:
            (logger.warning if state == STATE_DEAD else logger.info)(
                f"Codex 渠道掉号检测: provider={provider_id} {change}"
            )
        snap = snapshot(provider_id) or {}
        snap["usage"] = None
        return snap

    mark_alive(provider_id)
    snap = snapshot(provider_id) or {}
    snap["usage"] = usage
    return snap


async def _watchdog_loop(interval: int) -> None:
    while True:
        try:
            from db.selects.catalog import list_providers

            providers = await list_providers()
            for p in providers:
                if not (p.base_url or "").lower().startswith("codex://"):
                    continue
                if p.status != 1:
                    continue
                await check_channel(p.id, p.api_key_encrypted, p.base_url,
                                    p.timeout_seconds or 60)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 巡检自身不能把服务带崩
            logger.warning(f"Codex 掉号巡检异常（下轮重试）: {exc}")
        await asyncio.sleep(interval)


def start_watchdog(interval_seconds: int) -> None:
    """启动掉号巡检。interval_seconds <= 0 表示关闭（小部署靠流量侧被动检测就够）"""
    global _task
    if interval_seconds <= 0 or _task is not None:
        return
    _task = asyncio.create_task(_watchdog_loop(interval_seconds))
    logger.info(f"Codex 掉号巡检已启动：每 {interval_seconds}s 检查一轮")


def stop_watchdog() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
