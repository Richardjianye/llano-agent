"""每用户 MCP 上游凭据的所有权、加密、验证与审计边界。"""

from __future__ import annotations

import json
from typing import Any

from db import init_async_session
from mcp_adapters import get_adapter
from model.database import (CredentialAccessLog, McpInstance,
                            PlatformCredential, now)
from sqlalchemy import select
from utils.errors import (McpUpstreamError, NotFoundError, PermissionError_,
                          ValidationError_)
from utils.secret_box import (SecretDecryptError, SecretKeyMissing,
                              SecretPurpose, decrypt_secret, encrypt_secret)


def _config(instance: McpInstance) -> dict[str, Any]:
    try:
        value = json.loads(instance.config_json or "{}")
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


async def _resolve_instance(instance_code: str) -> McpInstance:
    maker = await init_async_session()
    async with maker() as session:
        instance = (await session.execute(
            select(McpInstance).where(McpInstance.code == instance_code)
        )).scalar_one_or_none()
    if not instance:
        raise NotFoundError("MCP 实例不存在")
    if instance.status != 1:
        raise PermissionError_("MCP 实例当前已停用")
    return instance


def _credential_platform(instance: McpInstance) -> str:
    adapter = get_adapter(instance.adapter_key)
    if adapter.permission_mode != "upstream_account" or not adapter.credential_platform:
        raise ValidationError_("该 MCP 实例不支持每用户上游账号绑定")
    return adapter.credential_platform


async def _load_row(session, uid: str, platform: str) -> PlatformCredential | None:
    return (await session.execute(select(PlatformCredential).where(
        PlatformCredential.user_uid == uid,
        PlatformCredential.platform == platform,
    ))).scalar_one_or_none()


def _status_payload(row: PlatformCredential | None) -> dict[str, Any]:
    if row is None:
        return {
            "configured": False,
            "status": "not_configured",
            "last_verified_at": None,
        }
    status = "verified" if row.status == 1 and row.last_verified_at else "invalid"
    return {
        "configured": True,
        "status": status,
        "last_verified_at": row.last_verified_at.isoformat()
        if row.last_verified_at else None,
    }


def _audit(
    uid: str,
    platform: str,
    action: str,
    request_id: str | None,
    client_ip: str | None,
) -> CredentialAccessLog:
    return CredentialAccessLog(
        operator_uid=uid,
        target_user_uid=uid,
        platform=platform,
        action=action,
        request_id=request_id,
        client_ip=client_ip,
    )


async def credential_status(uid: str, instance_code: str) -> dict[str, Any]:
    instance = await _resolve_instance(instance_code)
    platform = _credential_platform(instance)
    maker = await init_async_session()
    async with maker() as session:
        row = await _load_row(session, uid, platform)
        return _status_payload(row)


async def bind_and_verify(
    uid: str,
    instance_code: str,
    account: str,
    password: str,
    request_id: str | None,
    client_ip: str | None,
) -> dict[str, Any]:
    instance = await _resolve_instance(instance_code)
    platform = _credential_platform(instance)
    adapter = get_adapter(instance.adapter_key)
    auth = {"account": account.strip(), "password": password}
    await adapter.health(auth, _config(instance))
    try:
        encrypted = encrypt_secret(password, SecretPurpose.CREDENTIAL)
    except SecretKeyMissing as exc:
        raise ValidationError_(str(exc)) from exc

    verified_at = now()
    maker = await init_async_session()
    async with maker() as session:
        row = await _load_row(session, uid, platform)
        action = "update" if row else "create"
        if row:
            row.account = account.strip()
            row.secret_encrypted = encrypted
            row.status = 1
            row.last_verified_at = verified_at
        else:
            row = PlatformCredential(
                user_uid=uid,
                platform=platform,
                account=account.strip(),
                secret_encrypted=encrypted,
                key_version=1,
                status=1,
                last_verified_at=verified_at,
            )
            session.add(row)
        session.add(_audit(uid, platform, action, request_id, client_ip))
        await session.commit()
        return _status_payload(row)


async def verify_saved(
    uid: str,
    instance_code: str,
    request_id: str | None,
    client_ip: str | None,
) -> dict[str, Any]:
    instance = await _resolve_instance(instance_code)
    platform = _credential_platform(instance)
    maker = await init_async_session()
    async with maker() as session:
        row = await _load_row(session, uid, platform)
        if not row:
            raise PermissionError_("请先绑定自己的中台账号")
        try:
            password = decrypt_secret(row.secret_encrypted, SecretPurpose.CREDENTIAL)
        except (SecretDecryptError, SecretKeyMissing) as exc:
            raise ValidationError_("中台凭据无法解密，请重新绑定") from exc
        account = row.account
        credential_id = row.id
        encrypted_snapshot = row.secret_encrypted

    try:
        await get_adapter(instance.adapter_key).health(
            {"account": account, "password": password}, _config(instance)
        )
    except McpUpstreamError as exc:
        async with maker() as session:
            current = await _load_row(session, uid, platform)
            if (
                current
                and current.id == credential_id
                and current.account == account
                and current.secret_encrypted == encrypted_snapshot
                and exc.error_code in {
                    "datahub_auth_failed",
                    "datahub_interactive_verification_required",
                    "datahub_password_update_required",
                }
            ):
                current.status = 0
            session.add(_audit(uid, platform, "test", request_id, client_ip))
            await session.commit()
        raise

    async with maker() as session:
        current = await _load_row(session, uid, platform)
        if not current:
            raise PermissionError_("中台凭据已被清除")
        if (
            current.id == credential_id
            and current.account == account
            and current.secret_encrypted == encrypted_snapshot
        ):
            current.status = 1
            current.last_verified_at = now()
        session.add(_audit(uid, platform, "test", request_id, client_ip))
        await session.commit()
        return _status_payload(current)


async def delete_credential(
    uid: str,
    instance_code: str,
    request_id: str | None,
    client_ip: str | None,
) -> None:
    instance = await _resolve_instance(instance_code)
    platform = _credential_platform(instance)
    maker = await init_async_session()
    async with maker() as session:
        row = await _load_row(session, uid, platform)
        if not row:
            raise NotFoundError("尚未绑定该中台账号")
        await session.delete(row)
        session.add(_audit(uid, platform, "delete", request_id, client_ip))
        await session.commit()


async def load_for_execute(
    uid: str,
    instance: McpInstance,
    request_id: str | None,
) -> dict[str, str]:
    platform = _credential_platform(instance)
    maker = await init_async_session()
    async with maker() as session:
        row = await _load_row(session, uid, platform)
        if not row or row.status != 1 or not row.last_verified_at:
            raise PermissionError_("请先绑定并测试自己的中台账号")
        try:
            password = decrypt_secret(row.secret_encrypted, SecretPurpose.CREDENTIAL)
        except (SecretDecryptError, SecretKeyMissing) as exc:
            raise ValidationError_("中台凭据无法解密，请重新绑定") from exc
        session.add(_audit(uid, platform, "decrypt", request_id, None))
        await session.commit()
        return {"account": row.account, "password": password}
