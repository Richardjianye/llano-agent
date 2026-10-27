"""桌面端版本发布管理（仅 admin）

## 网关签不出清单

发布接口收的是「清单 + 离线签名机签好的签名」。网关只用配置里的公钥验一遍，
验过才入库。它没有私钥，因此：

- 后台被拿下，攻击者能下架版本、能发**别人签过的**旧清单，但签不出新的
- 数据库被写，改出来的清单同样过不了客户端那一关

验签放在这一层而不是 db/inserts：入口只有一个，责任就只在一处。两处都校验
看着更稳，实际是两处都以为对方做了。

## 已发布的清单不可编辑

只有上架/下架和删除。改内容签名就对不上，改签名等于凭空造签名 —— 既然网关
签不出清单，就不该有「修改已发布清单」这种接口。要改就下架旧的、重新签一版。
"""

import json

from config import get_settings
from db.inserts.release import insert_release
from db.selects.release import (get_release, get_release_by_target,
                                list_releases)
from db.updates.release import delete_release, set_release_status
from fastapi import APIRouter, Depends
from loguru import logger
from model.request.release import ReleasePublish, ReleaseStatusUpdate
from model.response import Response, UserInfo
from services.release_manifest import (ManifestError, normalize_manifest,
                                       public_key_fingerprint,
                                       verify_manifest)
from utils.dependencies import require_permission
from utils.errors import ConflictError, NotFoundError, ValidationError_

router = APIRouter(prefix="/release", tags=["桌面端版本发布"])


def _row_to_item(row) -> dict:
    return {
        "id": row.id,
        "version": row.version,
        "platform": row.platform,
        "arch": row.arch,
        "policy": row.policy,
        "min_supported_version": row.min_supported_version,
        "artifact_url": row.artifact_url,
        "artifact_sha256": row.artifact_sha256,
        "artifact_size": row.artifact_size,
        "release_notes": row.release_notes or "",
        "released_at": row.released_at,
        "public_key_fp": row.public_key_fp,
        "status": row.status,
        "published_by": row.published_by,
        "created_at": row.created_at.strftime("%Y-%m-%d %H:%M:%S") if row.created_at else None,
    }


@router.get("/list", response_model=Response)
async def release_list(
    platform: str = "",
    arch: str = "",
    _: UserInfo = Depends(require_permission("release:view")),
):
    """版本列表，按版本号从新到旧。"""
    rows = await list_releases(
        platform=platform.strip().lower() or None,
        arch=arch.strip().lower() or None,
    )
    settings = get_settings()
    return Response(
        data={
            "items": [_row_to_item(r) for r in rows],
            # 后台显示当前网关配的公钥指纹，方便与客户端内置的那把对照。
            # 两者不一致时，发出去的版本谁也装不上。
            "gateway_public_key_fp": public_key_fingerprint(settings.update_public_key),
        }
    )


@router.post("/publish", response_model=Response)
async def release_publish(
    body: ReleasePublish,
    operator: UserInfo = Depends(require_permission("release:edit")),
):
    """发布一个新版本。清单验签不过一律拒绝入库。"""
    public_key = (get_settings().update_public_key or "").strip()
    if not public_key:
        # 不配公钥就发不了版，而不是「跳过校验直接入库」：一个验不了签名的
        # 网关唯一安全的行为是拒绝发布。
        raise ValidationError_("网关未配置 UPDATE_PUBLIC_KEY，无法校验清单签名，拒绝发布")

    try:
        manifest = normalize_manifest(body.manifest)
    except ManifestError as exc:
        raise ValidationError_(f"清单字段不合法：{exc}")

    if not verify_manifest(manifest, body.signature, public_key):
        logger.warning(
            f"版本发布验签失败: version={manifest['version']} "
            f"{manifest['platform']}/{manifest['arch']} by={operator.uid}"
        )
        raise ValidationError_(
            "清单签名校验失败。确认签名机用的私钥与网关公钥是同一对密钥，"
            "且清单在签名之后没有被改动过"
        )

    existing = await get_release_by_target(
        manifest["version"], manifest["platform"], manifest["arch"]
    )
    if existing:
        raise ConflictError(
            f"{manifest['version']} / {manifest['platform']} / {manifest['arch']} 已存在，"
            "要替换请先删除旧条目"
        )

    release_id = await insert_release(
        manifest=manifest,
        signature=body.signature,
        public_key_fp=public_key_fingerprint(public_key) or "",
        published_by=operator.uid,
    )
    logger.info(
        f"桌面端版本已发布: id={release_id} version={manifest['version']} "
        f"{manifest['platform']}/{manifest['arch']} policy={manifest['policy']} "
        f"min={manifest['min_supported_version']} by={operator.uid}"
    )
    return Response(data={"id": release_id, "manifest": manifest})


@router.post("/status/{release_id}", response_model=Response)
async def release_set_status(
    release_id: int,
    body: ReleaseStatusUpdate,
    operator: UserInfo = Depends(require_permission("release:edit")),
):
    """上架/下架。下架后客户端立刻不再拿到这一版。"""
    if not await get_release(release_id):
        raise NotFoundError("版本不存在")
    await set_release_status(release_id, body.status)
    logger.info(f"桌面端版本状态变更: id={release_id} status={body.status} by={operator.uid}")
    return Response()


@router.post("/delete/{release_id}", response_model=Response)
async def release_delete(
    release_id: int,
    operator: UserInfo = Depends(require_permission("release:edit")),
):
    row = await get_release(release_id)
    if not row:
        raise NotFoundError("版本不存在")
    await delete_release(release_id)
    logger.info(
        f"桌面端版本已删除: id={release_id} version={row.version} "
        f"{row.platform}/{row.arch} by={operator.uid}"
    )
    return Response()


@router.get("/detail/{release_id}", response_model=Response)
async def release_detail(
    release_id: int,
    _: UserInfo = Depends(require_permission("release:view")),
):
    """含清单原文与签名，用于排查「客户端说验不过」这类问题。"""
    row = await get_release(release_id)
    if not row:
        raise NotFoundError("版本不存在")
    item = _row_to_item(row)
    item["manifest"] = json.loads(row.manifest_json)
    item["signature"] = row.signature
    return Response(data=item)
