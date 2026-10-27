"""桌面端更新检查 —— 客户端更新的唯一来源

## 为什么不要求登录

这个接口开机就打，且必须在登录之前也能打通：强制更新（U4 全屏阻断）要在
「客户端太旧、还没登录」的状态下也生效，否则「旧到登不上」的客户端恰好就是
更新不到的那一批。

代价是版本号、发行说明和安装包地址对能访问网关的人可见。可接受，因为：

- 接口一次只回**一条**清单，按 platform/arch/version 精确匹配，不能枚举
- 拿到清单也伪造不了更新 —— 签名是离线私钥签的，网关自己都签不出来
- 安装包本身的访问控制是构建物托管那一侧的事（阶段 6）

## 客户端上报的版本不可信

`current_version` 是客户端自己填的，填个 999 就不会收到更新。这不是漏洞，
是它本来就在自己的机器上跑自己的代码 —— 想不更新根本不用骗网关。真正的
防线在服务端：过期客户端该被拒绝的业务请求由网关按自己的规则拒（U5），
不依赖它诚实上报版本。

## 发出去之前再验一次签

库里的行在返回前用公钥重验一遍。这拦不住有私钥的人（没有这种人），也不是
客户端的替代品（客户端一定会自己验）——它的作用是**让问题出现在网关日志里**，
而不是同时出现在两百台机器上，且现象是各说各话的「验签失败」。
"""

import json

from config import get_settings
from db.selects.release import latest_release_for
from fastapi import APIRouter, Query, Response as FastAPIResponse
from loguru import logger
from services.release_manifest import (ARCHES, PLATFORMS, ManifestError,
                                       manifest_applies_to, parse_version,
                                       verify_manifest)
from utils.errors import ValidationError_

router = APIRouter()


@router.get("/client-update")
async def client_update(
    response: FastAPIResponse,
    platform: str = Query(..., description="darwin / win32"),
    arch: str = Query(..., description="x64 / arm64"),
    version: str = Query(..., description="客户端当前版本"),
):
    response.headers["Cache-Control"] = "no-store"

    normalized_platform = platform.strip().lower()
    normalized_arch = arch.strip().lower()
    if normalized_platform not in PLATFORMS:
        raise ValidationError_(f"不支持的平台：{platform}")
    if normalized_arch not in ARCHES:
        raise ValidationError_(f"不支持的架构：{arch}")
    try:
        parse_version(version)
    except ManifestError as exc:
        # 明确报错而不是当成「没有更新」：客户端版本号坏掉是它自己的 bug，
        # 静默返回没有更新会让它永远停在旧版且毫无提示。
        raise ValidationError_(str(exc))

    row = await latest_release_for(normalized_platform, normalized_arch)
    if row is None:
        return {"update": None}

    manifest = json.loads(row.manifest_json)

    public_key = (get_settings().update_public_key or "").strip()
    if not verify_manifest(manifest, row.signature, public_key):
        logger.error(
            f"版本清单在下发前验签失败，已按「无更新」处理: id={row.id} "
            f"version={row.version} {row.platform}/{row.arch} "
            f"fp_in_db={row.public_key_fp} —— 检查 UPDATE_PUBLIC_KEY 是否被改过，"
            "或该行是否被直接改库"
        )
        return {"update": None}

    if not manifest_applies_to(
        manifest,
        platform=normalized_platform,
        arch=normalized_arch,
        current_version=version,
    ):
        return {"update": None}

    # 只回清单和签名。**不回「是否强制」这类网关算出来的字段**：那是没进签名
    # 的、可被中间人改的一个布尔值，客户端一旦拿它当判据，强制更新就成了摆设。
    # 客户端用 update_is_mandatory() 自己从已验签的清单里算。
    return {"update": {"manifest": manifest, "signature": row.signature}}
