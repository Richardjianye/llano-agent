"""MCP 实例、能力目录、授权判定与审计的唯一服务层。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional

from db import init_async_session
from mcp_adapters import get_adapter
from mcp_adapters.base import Capability
from model.database import (McpCallLog, McpCapability, McpInstance,
                            McpUserPermission, PlatformCredential, User, now)
from services import mcp_credentials
from sqlalchemy import delete, func, or_, select, update
from utils.errors import (ConflictError, NotFoundError, PermissionError_,
                          ValidationError_)
from utils.secret_box import (SecretDecryptError, SecretKeyMissing,
                              SecretPurpose, decrypt_secret, encrypt_secret)


@dataclass(frozen=True)
class ResolvedCapability:
    instance: McpInstance
    db_capability: McpCapability
    trusted: Capability
    auth: dict[str, Any]
    constraints: dict[str, Any]


@dataclass(frozen=True)
class CatalogEntry:
    instance: McpInstance
    capability: McpCapability
    access_status: str


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(value: Optional[str], fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _split_config(adapter, auth: Mapping[str, Any], config: Mapping[str, Any]) -> tuple[dict, dict]:
    secret_keys = {field["key"] for field in adapter.config_schema() if field.get("secret")}
    clean_auth = {key: value for key, value in auth.items() if key in secret_keys and value not in (None, "")}
    clean_config = {key: value for key, value in config.items() if key not in secret_keys}
    clean_config["_configured_secret_fields"] = sorted(clean_auth)
    return clean_auth, clean_config


async def create_instance(code: str, name: str, adapter_key: str, auth: dict, config: dict, remark: str | None) -> int:
    adapter = get_adapter(adapter_key)
    clean_auth, clean_config = _split_config(adapter, auth, config)
    adapter.validate_config(clean_auth, clean_config)
    try:
        encrypted = encrypt_secret(_json(clean_auth), SecretPurpose.CREDENTIAL)
    except SecretKeyMissing as exc:
        raise ValidationError_(str(exc)) from exc
    maker = await init_async_session()
    async with maker() as session:
        exists = (await session.execute(select(McpInstance.id).where(McpInstance.code == code))).scalar_one_or_none()
        if exists:
            raise ConflictError(f"MCP 实例编码 {code} 已存在")
        instance = McpInstance(code=code, name=name, adapter_key=adapter_key,
                               auth_config_encrypted=encrypted, config_json=_json(clean_config),
                               status=1, remark=remark)
        session.add(instance)
        await session.flush()
        instance_id = instance.id
        await session.commit()
    await sync_capabilities(instance_id)
    return instance_id


async def update_instance(instance_id: int, *, name=None, auth=None, config=None, status=None, remark=None) -> None:
    maker = await init_async_session()
    async with maker() as session:
        instance = await session.get(McpInstance, instance_id)
        if not instance:
            raise NotFoundError("MCP 实例不存在")
        adapter = get_adapter(instance.adapter_key)
        current_auth = decrypt_auth(instance)
        current_config = _loads(instance.config_json, {})
        current_config.pop("_configured_secret_fields", None)
        if auth is not None:
            current_auth.update({k: v for k, v in auth.items() if v not in (None, "")})
        if config is not None:
            current_config.update(config)
        clean_auth, clean_config = _split_config(adapter, current_auth, current_config)
        adapter.validate_config(clean_auth, clean_config)
        values: dict[str, Any] = {"updated_at": now(), "config_json": _json(clean_config)}
        if auth is not None:
            values["auth_config_encrypted"] = encrypt_secret(_json(clean_auth), SecretPurpose.CREDENTIAL)
        if name is not None: values["name"] = name
        if status is not None: values["status"] = status
        if remark is not None: values["remark"] = remark
        await session.execute(update(McpInstance).where(McpInstance.id == instance_id).values(values))
        await session.commit()


def decrypt_auth(instance: McpInstance) -> dict:
    try:
        return _loads(decrypt_secret(instance.auth_config_encrypted, SecretPurpose.CREDENTIAL), {})
    except (SecretDecryptError, SecretKeyMissing) as exc:
        raise ValidationError_(f"MCP 实例凭证无法解密：{exc}") from exc


async def list_instances() -> list[McpInstance]:
    maker = await init_async_session()
    async with maker() as session:
        return list((await session.execute(select(McpInstance).order_by(McpInstance.id))).scalars())


async def get_instance(instance_id: int) -> McpInstance | None:
    maker = await init_async_session()
    async with maker() as session:
        return await session.get(McpInstance, instance_id)


async def sync_capabilities(instance_id: int) -> dict[str, int]:
    maker = await init_async_session()
    async with maker() as session:
        instance = await session.get(McpInstance, instance_id)
        if not instance:
            raise NotFoundError("MCP 实例不存在")
        adapter = get_adapter(instance.adapter_key)
        manifest = {cap.code: cap for cap in adapter.capabilities()}
        existing = {
            row.capability_code: row
            for row in (await session.execute(select(McpCapability).where(McpCapability.instance_id == instance_id))).scalars()
        }
        existing_by_endpoint = {(row.method, row.path): row for row in existing.values()}
        renamed_by_code = {
            code: old
            for code, cap in manifest.items()
            if code not in existing
            and (old := existing_by_endpoint.get((cap.method, cap.path))) is not None
            and old.capability_code not in manifest
        }
        added = changed_count = renamed = 0
        for code, cap in manifest.items():
            renamed_row = renamed_by_code.get(code)
            changed = code in existing and existing[code].source_hash != cap.source_hash
            values = dict(name=cap.name, domain=cap.domain, description=cap.metadata.get("doc_url"),
                          method=cap.method, path=cap.path, input_schema_json=_json(cap.input_schema),
                          metadata_json=_json(cap.metadata), source_hash=cap.source_hash,
                          updated_at=now())
            if code in existing:
                # 已授权代号若被文档更新成另一份定义，先停用等待管理员复核，
                # 绝不能让旧授权静默指向变化后的接口。
                if changed:
                    values["status"] = 0
                    changed_count += 1
                await session.execute(update(McpCapability).where(McpCapability.id == existing[code].id).values(values))
            elif renamed_row:
                # 文档分类从中文补齐稳定英文段时，method/path 没变，属于短码修复。
                # 原位改码可以保留 capability_id，从而不破坏已有用户授权。
                await session.execute(update(McpCapability).where(
                    McpCapability.id == renamed_row.id,
                ).values(capability_code=code, **values))
                renamed += 1
            else:
                # 首次导入的是已经过离线审计的基线；后续目录新增则默认停用，
                # 由管理员在目录页明确启用。
                initial_import = not existing
                session.add(McpCapability(instance_id=instance_id, capability_code=code,
                                           status=1 if initial_import else 0, **values))
                added += 1
        renamed_old_codes = {row.capability_code for row in renamed_by_code.values()}
        stale = set(existing) - set(manifest) - renamed_old_codes
        if stale:
            await session.execute(update(McpCapability).where(
                McpCapability.instance_id == instance_id,
                McpCapability.capability_code.in_(stale),
            ).values(status=0, updated_at=now()))
        await session.commit()
        return {"total": len(manifest), "added": added, "changed": changed_count,
                "renamed": renamed,
                "unavailable": len(stale)}


async def preview_capability_sync(instance_id: int) -> dict[str, Any]:
    maker = await init_async_session()
    async with maker() as session:
        instance = await session.get(McpInstance, instance_id)
        if not instance:
            raise NotFoundError("MCP 实例不存在")
        manifest = {cap.code: cap for cap in get_adapter(instance.adapter_key).capabilities()}
        existing = {row.capability_code: row for row in (await session.execute(
            select(McpCapability).where(McpCapability.instance_id == instance_id)
        )).scalars()}
        added = sorted(set(manifest) - set(existing))
        unavailable = sorted(set(existing) - set(manifest))
        existing_by_endpoint = {(row.method, row.path): row for row in existing.values()}
        renamed = []
        for code in list(added):
            cap = manifest[code]
            old = existing_by_endpoint.get((cap.method, cap.path))
            if old and old.capability_code not in manifest:
                renamed.append({"from": old.capability_code, "to": code})
                added.remove(code)
                if old.capability_code in unavailable:
                    unavailable.remove(old.capability_code)
        changed = sorted(code for code in set(manifest) & set(existing)
                         if manifest[code].source_hash != existing[code].source_hash)
        return {"total": len(manifest), "added": added, "changed": changed,
                "renamed": renamed,
                "unavailable": unavailable}


async def check_health(instance_id: int) -> dict[str, Any]:
    instance = await get_instance(instance_id)
    if not instance:
        raise NotFoundError("MCP 实例不存在")
    return await get_adapter(instance.adapter_key).health(
        decrypt_auth(instance), _loads(instance.config_json, {}))


async def set_capability_status(instance_id: int, capability_id: int, status: int) -> None:
    maker = await init_async_session()
    async with maker() as session:
        result = await session.execute(update(McpCapability).where(
            McpCapability.id == capability_id,
            McpCapability.instance_id == instance_id,
        ).values(status=status, updated_at=now()))
        if result.rowcount != 1:
            raise NotFoundError("MCP 能力不存在")
        await session.commit()


async def list_capabilities(instance_id: int, query: str | None = None, domain: str | None = None, enabled_only=False) -> list[McpCapability]:
    maker = await init_async_session()
    async with maker() as session:
        stmt = select(McpCapability).where(McpCapability.instance_id == instance_id)
        if query:
            needle = f"%{query}%"
            stmt = stmt.where(or_(McpCapability.capability_code.like(needle), McpCapability.name.like(needle)))
        if domain: stmt = stmt.where(McpCapability.domain == domain)
        if enabled_only: stmt = stmt.where(McpCapability.status == 1)
        stmt = stmt.order_by(McpCapability.domain, McpCapability.capability_code)
        return list((await session.execute(stmt)).scalars())


async def replace_permissions(user_uid: str, instance_id: int, codes: list[str], effect: str,
                              constraints: dict, expires_at: datetime | None, granted_by: str,
                              remark: str | None = None) -> int:
    maker = await init_async_session()
    async with maker() as session:
        instance = await session.get(McpInstance, instance_id)
        if not instance:
            raise NotFoundError("MCP 实例不存在")
        if get_adapter(instance.adapter_key).permission_mode != "local_grants":
            raise ValidationError_("该 MCP 实例不使用本地能力授权")
        if not await session.scalar(select(User.id).where(User.uid == user_uid, User.status == 1)):
            raise NotFoundError("授权用户不存在或已停用")
        caps = list((await session.execute(select(McpCapability).where(
            McpCapability.instance_id == instance_id,
            McpCapability.capability_code.in_(codes) if codes else False,
            McpCapability.status == 1,
        ))).scalars())
        if len(caps) != len(set(codes)):
            raise ValidationError_("授权列表包含不存在或已停用的能力短码")
        await session.execute(delete(McpUserPermission).where(
            McpUserPermission.user_uid == user_uid, McpUserPermission.instance_id == instance_id))
        for cap in caps:
            session.add(McpUserPermission(user_uid=user_uid, instance_id=instance_id,
                capability_id=cap.id, effect=effect, constraints_json=_json(constraints),
                expires_at=expires_at, status=1, granted_by=granted_by, remark=remark))
        await session.commit()
        return len(caps)


async def list_permissions(instance_id: int, user_uid: str | None = None) -> list[tuple[McpUserPermission, McpCapability, User]]:
    maker = await init_async_session()
    async with maker() as session:
        stmt = (select(McpUserPermission, McpCapability, User)
                .join(McpCapability, McpCapability.id == McpUserPermission.capability_id)
                .join(User, User.uid == McpUserPermission.user_uid)
                .where(McpUserPermission.instance_id == instance_id))
        if user_uid: stmt = stmt.where(McpUserPermission.user_uid == user_uid)
        return list((await session.execute(stmt.order_by(User.username, McpCapability.capability_code))).all())


def validate_arguments(cap: Capability, arguments: Mapping[str, Any]) -> None:
    schema = cap.input_schema
    properties = schema.get("properties", {})
    unknown = set(arguments) - set(properties)
    if schema.get("additionalProperties") is False and unknown:
        raise ValidationError_(f"{cap.code} 不接受参数：{', '.join(sorted(unknown))}")
    missing = [key for key in schema.get("required", []) if key not in arguments]
    if missing:
        raise ValidationError_(f"{cap.code} 缺少参数：{', '.join(missing)}")
    python_types = {
        "string": str, "integer": int, "number": (int, float),
        "array": list, "object": dict, "boolean": bool,
    }
    for key, value in arguments.items():
        expected = python_types.get(properties.get(key, {}).get("type"))
        if expected and (not isinstance(value, expected) or
                         properties[key].get("type") == "integer" and isinstance(value, bool)):
            raise ValidationError_(f"参数 {key} 类型不符合接口 Schema")


def enforce_constraints(arguments: Mapping[str, Any], constraints: Mapping[str, Any]) -> None:
    for field, allowed_key in (("sid", "allowed_sids"), ("sid_list", "allowed_sids"),
                               ("sidList", "allowed_sids"), ("store_id", "allowed_store_ids"),
                               ("storeIds", "allowed_store_ids"), ("shopIds", "allowed_store_ids"),
                               ("warehouse_id", "allowed_warehouse_ids"),
                               ("warehouseId", "allowed_warehouse_ids")):
        if field not in arguments or allowed_key not in constraints:
            continue
        values = arguments[field] if isinstance(arguments[field], list) else [arguments[field]]
        allowed = {str(v) for v in constraints[allowed_key]}
        if any(str(v) not in allowed for v in values):
            raise PermissionError_(f"参数 {field} 超出管理员授权范围")
    maximum = constraints.get("max_page_size")
    if maximum is not None:
        for field in ("length", "page_size", "pageSize"):
            if field in arguments and int(arguments[field]) > int(maximum):
                raise PermissionError_(f"参数 {field} 超过授权上限 {maximum}")
    max_days = constraints.get("max_days")
    if max_days is not None:
        pairs = (("start_date", "end_date"), ("startDate", "endDate"),
                 ("startDateTime", "endDateTime"))
        for start_key, end_key in pairs:
            if start_key in arguments and end_key in arguments:
                try:
                    start = datetime.fromisoformat(str(arguments[start_key])).date()
                    end = datetime.fromisoformat(str(arguments[end_key])).date()
                except ValueError as exc:
                    raise ValidationError_("时间范围格式不合法") from exc
                if (end - start).days > int(max_days):
                    raise PermissionError_(f"查询时间范围超过授权上限 {max_days} 天")


async def resolve_capability(
    uid: str,
    code: str,
    instance_code: str | None = None,
    request_id: str | None = None,
) -> ResolvedCapability:
    maker = await init_async_session()
    async with maker() as session:
        stmt = (
            select(McpInstance, McpCapability)
            .join(McpCapability, McpCapability.instance_id == McpInstance.id)
            .where(McpCapability.capability_code == code,
                   McpInstance.status == 1, McpCapability.status == 1,
            )
        )
        if instance_code:
            stmt = stmt.where(McpInstance.code == instance_code)
        rows = (await session.execute(stmt)).all()

        local_ids = [
            db_cap.id for instance, db_cap in rows
            if get_adapter(instance.adapter_key).permission_mode == "local_grants"
        ]
        permission_by_capability: dict[int, McpUserPermission] = {}
        if local_ids:
            permissions = (await session.execute(select(McpUserPermission).where(
                McpUserPermission.user_uid == uid,
                McpUserPermission.capability_id.in_(local_ids),
                McpUserPermission.status == 1,
            ))).scalars()
            permission_by_capability = {
                permission.capability_id: permission for permission in permissions
            }

        checked_at = now()
        eligible = []
        for instance, db_cap in rows:
            adapter = get_adapter(instance.adapter_key)
            permission = permission_by_capability.get(db_cap.id)
            if adapter.permission_mode == "local_grants" and (
                not permission
                or permission.effect != "allow"
                or permission.expires_at and permission.expires_at <= checked_at
            ):
                continue
            eligible.append((instance, db_cap, permission))

        if len(eligible) > 1:
            raise ValidationError_("该能力短码存在于多个实例，请同时传 instance_code")
        row = eligible[0] if eligible else None
        if not row:
            raise PermissionError_(f"未获授权的 MCP 能力：{code}")
        instance, db_cap, permission = row
        trusted = {cap.code: cap for cap in get_adapter(instance.adapter_key).capabilities()}.get(code)
        if not trusted or trusted.source_hash != db_cap.source_hash:
            raise PermissionError_("MCP 能力目录与受信任适配器不一致，请管理员重新同步")

        adapter = get_adapter(instance.adapter_key)
        if adapter.permission_mode == "local_grants":
            auth = decrypt_auth(instance)
            constraints = _loads(permission.constraints_json, {})
        elif adapter.permission_mode == "upstream_account":
            auth = await mcp_credentials.load_for_execute(uid, instance, request_id)
            constraints = {}
        else:
            raise PermissionError_("MCP 适配器权限模式不受支持")
        return ResolvedCapability(instance, db_cap, trusted, auth, constraints)


async def search_catalog(uid: str, query: str | None, instance_code: str | None,
                         domain: str | None, limit: int) -> list[CatalogEntry]:
    maker = await init_async_session()
    async with maker() as session:
        stmt = (select(McpInstance, McpCapability)
                .join(McpCapability, McpCapability.instance_id == McpInstance.id))
        if query:
            needle = f"%{query}%"
            stmt = stmt.where(or_(McpCapability.capability_code.like(needle),
                                  McpCapability.name.like(needle),
                                  McpCapability.domain.like(needle)))
        if instance_code: stmt = stmt.where(McpInstance.code == instance_code)
        if domain: stmt = stmt.where(McpCapability.domain == domain)
        stmt = stmt.order_by(McpInstance.code, McpCapability.domain,
                             McpCapability.capability_code)
        # 分类首层需要先看到完整目录才能给出准确计数；接口层下钻才应用 limit。
        if query or domain:
            stmt = stmt.limit(limit)
        rows = list((await session.execute(stmt)).all())

        local_capability_ids = [capability.id for instance, capability in rows
                                if get_adapter(instance.adapter_key).permission_mode == "local_grants"]
        permissions = []
        if local_capability_ids:
            permissions = list((await session.execute(select(McpUserPermission).where(
                McpUserPermission.user_uid == uid,
                McpUserPermission.capability_id.in_(local_capability_ids),
            ))).scalars())
        permission_by_capability = {row.capability_id: row for row in permissions}

        upstream_platforms = {
            get_adapter(instance.adapter_key).credential_platform
            for instance, _capability in rows
            if get_adapter(instance.adapter_key).permission_mode == "upstream_account"
            and get_adapter(instance.adapter_key).credential_platform
        }
        verified_platforms: set[str] = set()
        if upstream_platforms:
            verified_platforms = set((await session.execute(
                select(PlatformCredential.platform).where(
                    PlatformCredential.user_uid == uid,
                    PlatformCredential.platform.in_(upstream_platforms),
                    PlatformCredential.status == 1,
                    PlatformCredential.last_verified_at.is_not(None),
                )
            )).scalars())

        result = []
        checked_at = now()
        for instance, capability in rows:
            adapter = get_adapter(instance.adapter_key)
            trusted = {item.code: item for item in adapter.capabilities()}.get(
                capability.capability_code
            )
            if (instance.status != 1 or capability.status != 1 or not trusted or
                    trusted.source_hash != capability.source_hash):
                status = "unavailable"
            elif adapter.permission_mode == "upstream_account":
                status = ("available" if adapter.credential_platform in verified_platforms
                          else "setup_required")
            else:
                permission = permission_by_capability.get(capability.id)
                if not permission or permission.status != 1 or permission.effect != "allow":
                    status = "request_access"
                elif permission.expires_at and permission.expires_at <= checked_at:
                    status = "expired"
                else:
                    status = "available"
            result.append(CatalogEntry(instance, capability, status))
        return result


async def record_call(*, request_id: str, uid: str, api_key_id: int | None, code: str,
                      arguments: Mapping[str, Any], status: str, error_code: str | None,
                      latency_ms: int, response_size: int = 0, instance_id=None, capability_id=None,
                      upstream_request_id: str | None = None) -> None:
    redacted = {k: ("***" if any(word in k.lower() for word in ("secret", "token", "password")) else v)
                for k, v in arguments.items()}
    maker = await init_async_session()
    async with maker() as session:
        session.add(McpCallLog(request_id=request_id, user_uid=uid, api_key_id=api_key_id,
            instance_id=instance_id, capability_id=capability_id, capability_code=code,
            args_json=_json(redacted), status=status, error_code=error_code,
            upstream_request_id=upstream_request_id,
            latency_ms=latency_ms, response_size=response_size))
        await session.commit()


async def list_call_logs(instance_id: int, limit: int = 100) -> list[tuple[McpCallLog, User | None]]:
    maker = await init_async_session()
    async with maker() as session:
        stmt = (select(McpCallLog, User)
                .outerjoin(User, User.uid == McpCallLog.user_uid)
                .where(McpCallLog.instance_id == instance_id)
                .order_by(McpCallLog.id.desc())
                .limit(limit))
        return list((await session.execute(stmt)).all())
