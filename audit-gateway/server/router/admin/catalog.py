"""模型目录与白名单管理路由（仅 admin）

上游 API Key 只进不出：写入时明文接收、加密入库，读取时一律脱敏。
"""

from db.inserts.catalog import (insert_model, insert_provider,
                                insert_whitelist_entries,
                                insert_whitelist_entry)
from db.selects.catalog import (get_model, get_provider, get_provider_by_code,
                                get_whitelist_entry, list_models,
                                list_providers, list_whitelist,
                                load_access_snapshot)
from db.selects.department import get_department
from db.selects.user import get_user_info
from db.updates.catalog import (UNSET, delete_whitelist_by_model,
                                delete_whitelist_entries,
                                delete_whitelist_entry, update_model,
                                update_provider, update_whitelist_entry)
from fastapi import APIRouter, Depends
from loguru import logger
from model.database import (ALL_ROLES, SCOPE_DEPT, SCOPE_GLOBAL, SCOPE_ROLE,
                            SCOPE_USER)
from model.request.org import (ModelCreate, ModelUpdate, ProviderCreate,
                               ProviderUpdate, WhitelistBatchCreate,
                               WhitelistBatchDelete, WhitelistCreate,
                               WhitelistUpdate)
from model.response import (ModelAdminInfo, ProviderInfo, Response, UserInfo,
                            WhitelistEntryInfo)
from services.model_access import WhitelistRule, model_public_name
from services.model_grants import (MAX_BATCH_ROWS, DeptNode, GrantTarget,
                                   MemberRef, dept_path, member_access_report,
                                   model_grant_overview, plan_batch_grants)
from services.image_generation import is_image_model
from sqlalchemy.exc import IntegrityError
from utils.dependencies import require_permission
from utils.errors import ConflictError, NotFoundError, ValidationError_
from utils.secret_box import SecretKeyMissing

router = APIRouter(prefix="/catalog", tags=["模型与白名单管理"])


# ==================== 上游供应商 ====================


@router.get("/provider/list", response_model=Response)
async def provider_list(_: UserInfo = Depends(require_permission("model:view"))):
    """供应商列表。api_key 仅返回掩码"""
    providers = await list_providers()
    items = [
        ProviderInfo(
            id=p.id,
            code=p.code,
            name=p.name,
            base_url=p.base_url,
            kind="codex" if p.base_url.lower().startswith("codex://") else "openai",
            api_key_masked="已配置" if p.api_key_encrypted else "未配置",
            timeout_seconds=p.timeout_seconds,
            status=p.status,
            remark=p.remark,
        ).model_dump()
        for p in providers
    ]
    return Response(data={"items": items})


@router.post("/provider/create", response_model=Response)
async def provider_create(
    body: ProviderCreate,
    operator: UserInfo = Depends(require_permission("model:edit")),
):
    """新增上游供应商（DeepSeek 直连 / 合作厂商 NewAPI）"""
    if body.base_url.strip().lower().startswith("codex://"):
        raise ValidationError_("Codex 渠道请通过专用授权入口创建")
    if await get_provider_by_code(body.code):
        raise ConflictError(f"供应商编码 {body.code} 已存在")

    try:
        provider_id = await insert_provider(
            code=body.code,
            name=body.name,
            base_url=body.base_url.strip().rstrip("/"),
            api_key=body.api_key,
            timeout_seconds=body.timeout_seconds,
            remark=body.remark,
        )
    except SecretKeyMissing as exc:
        raise ValidationError_(str(exc))

    logger.info(f"上游供应商已创建: id={provider_id} code={body.code} by={operator.uid}")
    return Response(data={"id": provider_id})


@router.post("/provider/update/{provider_id}", response_model=Response)
async def provider_update(
    provider_id: int,
    body: ProviderUpdate,
    operator: UserInfo = Depends(require_permission("model:edit")),
):
    """修改供应商。api_key 留空表示不改动"""
    provider = await get_provider(provider_id)
    if not provider:
        raise NotFoundError("供应商不存在")
    codex = provider.base_url.lower().startswith("codex://")
    if codex and (body.api_key or (body.base_url and body.base_url.rstrip("/") != provider.base_url.rstrip("/"))):
        raise ValidationError_("Codex 凭据与地址请通过专用授权入口修改；其他设置仍可修改")
    if not codex and body.base_url and body.base_url.strip().lower().startswith("codex://"):
        raise ValidationError_("普通渠道不能切换为 Codex，请通过专用入口授权")

    try:
        await update_provider(
            provider_id,
            name=body.name,
            base_url=body.base_url.strip().rstrip("/") if body.base_url else None,
            api_key=body.api_key,
            timeout_seconds=body.timeout_seconds,
            status=body.status,
            remark=body.remark,
        )
    except SecretKeyMissing as exc:
        raise ValidationError_(str(exc))

    logger.info(f"上游供应商已更新: id={provider_id} by={operator.uid}")
    return Response()


# ==================== 模型 ====================


@router.get("/model/list", response_model=Response)
async def model_list(
    provider_id: int = None,
    _: UserInfo = Depends(require_permission("model:view")),
):
    """模型列表（管理端视角，含上游真实模型名）"""
    rows = await list_models(provider_id=provider_id)
    items = [
        ModelAdminInfo(
            id=m.id,
            provider_id=m.provider_id,
            provider_code=code,
            capability="image" if is_image_model(m) else "chat",
            model_name=m.model_name,
            display_name=m.display_name,
            alias=m.alias,
            supports_stream=bool(m.supports_stream),
            supports_multimodal=bool(m.supports_multimodal),
            max_context_tokens=m.max_context_tokens,
            billing_mode=m.billing_mode or "token",
            input_price_per_1m=(
                float(m.input_price_per_1m) if m.input_price_per_1m is not None else None
            ),
            output_price_per_1m=(
                float(m.output_price_per_1m) if m.output_price_per_1m is not None else None
            ),
            cached_input_price_per_1m=(
                float(m.cached_input_price_per_1m)
                if m.cached_input_price_per_1m is not None else None
            ),
            price_per_call=(
                float(m.price_per_call) if m.price_per_call is not None else None
            ),
            currency=m.currency or "CNY",
            status=m.status,
            remark=m.remark,
        ).model_dump()
        for m, code in rows
    ]
    return Response(data={"items": items})


@router.post("/model/create", response_model=Response)
async def model_create(
    body: ModelCreate,
    operator: UserInfo = Depends(require_permission("model:edit")),
):
    """登记一个可路由的模型"""
    if not await get_provider(body.provider_id):
        raise NotFoundError("供应商不存在")

    try:
        model_id = await insert_model(
            provider_id=body.provider_id,
            model_name=body.model_name,
            display_name=body.display_name,
            alias=body.alias or None,
            supports_stream=body.supports_stream,
            supports_multimodal=body.supports_multimodal,
            max_context_tokens=body.max_context_tokens,
            billing_mode=body.billing_mode,
            input_price_per_1m=body.input_price_per_1m,
            output_price_per_1m=body.output_price_per_1m,
            cached_input_price_per_1m=body.cached_input_price_per_1m,
            price_per_call=body.price_per_call,
            currency=body.currency,
            remark=body.remark,
        )
    except IntegrityError as exc:
        raise ConflictError("该渠道模型或对外别名已存在，请刷新目录后重试") from exc
    logger.info(f"模型已登记: id={model_id} name={body.model_name} by={operator.uid}")
    return Response(data={"id": model_id})


@router.post("/model/update/{model_id}", response_model=Response)
async def model_update(
    model_id: int,
    body: ModelUpdate,
    operator: UserInfo = Depends(require_permission("model:edit")),
):
    """修改模型配置"""
    if not await get_model(model_id):
        raise NotFoundError("模型不存在")

    # 单价的 null 是有含义的（清空为未配置），所以要区分「传了 null」和「没传」，
    # 靠 model_fields_set 判断，没传的一律给 UNSET 让 DAO 跳过该列
    fields = body.model_fields_set
    await update_model(
        model_id,
        display_name=body.display_name,
        # 别名的 null 与单价同理：传了 null = 清空，没传 = 不改动
        alias=(body.alias if "alias" in fields else UNSET),
        supports_stream=body.supports_stream,
        supports_multimodal=body.supports_multimodal,
        max_context_tokens=body.max_context_tokens,
        input_price_per_1m=(
            body.input_price_per_1m if "input_price_per_1m" in fields else UNSET
        ),
        output_price_per_1m=(
            body.output_price_per_1m if "output_price_per_1m" in fields else UNSET
        ),
        cached_input_price_per_1m=(
            body.cached_input_price_per_1m
            if "cached_input_price_per_1m" in fields else UNSET
        ),
        price_per_call=(
            body.price_per_call if "price_per_call" in fields else UNSET
        ),
        billing_mode=body.billing_mode,
        currency=body.currency,
        status=body.status,
        remark=body.remark,
    )
    logger.info(f"模型已更新: id={model_id} by={operator.uid}")
    return Response()


@router.post("/model/delete/{model_id}", response_model=Response)
async def model_delete(
    model_id: int,
    operator: UserInfo = Depends(require_permission("model:edit")),
):
    """停用模型并清理其全部白名单授权（不物理删模型，保留审计追溯）"""
    if not await get_model(model_id):
        raise NotFoundError("模型不存在")

    await update_model(model_id, status=0)
    await delete_whitelist_by_model(model_id)
    logger.info(f"模型已停用并清理授权: id={model_id} by={operator.uid}")
    return Response()


# ==================== 白名单 ====================


async def _validate_scope(scope: str, scope_value: str) -> str:
    """校验作用域取值，返回规范化后的 scope_value"""
    if scope == SCOPE_GLOBAL:
        return ""

    if not scope_value:
        raise ValidationError_(f"作用域 {scope} 必须提供 scope_value")

    if scope == SCOPE_DEPT:
        if not scope_value.isdigit() or not await get_department(int(scope_value)):
            raise NotFoundError("部门不存在")
    elif scope == SCOPE_ROLE:
        if scope_value not in ALL_ROLES:
            raise ValidationError_(f"角色非法，可选值：{', '.join(ALL_ROLES)}")
    elif scope == SCOPE_USER:
        if not await get_user_info(uid=scope_value):
            raise NotFoundError("用户不存在")

    return scope_value


@router.get("/whitelist/list", response_model=Response)
async def whitelist_list(
    scope: str = None,
    scope_value: str = None,
    _: UserInfo = Depends(require_permission("model:view")),
):
    """白名单条目列表"""
    rows = await list_whitelist(scope=scope, scope_value=scope_value)
    items = [
        WhitelistEntryInfo(
            id=r.id,
            scope=r.scope,
            scope_value=r.scope_value,
            model_id=r.model_id,
            allowed=bool(r.allowed),
            remark=r.remark,
        ).model_dump()
        for r in rows
    ]
    return Response(data={"items": items})


@router.post("/whitelist/create", response_model=Response)
async def whitelist_create(
    body: WhitelistCreate,
    operator: UserInfo = Depends(require_permission("model:edit")),
):
    """新增授权/拒绝条目"""
    model = await get_model(body.model_id)
    if not model:
        raise NotFoundError("模型不存在")

    scope_value = await _validate_scope(body.scope, body.scope_value)

    existing = await list_whitelist(scope=body.scope, scope_value=scope_value)
    if any(e.model_id == body.model_id for e in existing):
        raise ConflictError("该作用域下已存在同一模型的授权条目")

    entry_id = await insert_whitelist_entry(
        scope=body.scope,
        scope_value=scope_value,
        model_id=body.model_id,
        allowed=body.allowed,
        remark=body.remark,
    )
    logger.info(
        f"白名单条目已创建: id={entry_id} scope={body.scope}:{scope_value} "
        f"model={model_public_name(model)} allowed={body.allowed} by={operator.uid}"
    )
    return Response(data={"id": entry_id})


@router.post("/whitelist/update/{entry_id}", response_model=Response)
async def whitelist_update(
    entry_id: int,
    body: WhitelistUpdate,
    operator: UserInfo = Depends(require_permission("model:edit")),
):
    """修改条目（主要用于在允许/显式拒绝之间切换）"""
    if not await get_whitelist_entry(entry_id):
        raise NotFoundError("白名单条目不存在")

    await update_whitelist_entry(entry_id, allowed=body.allowed, remark=body.remark)
    logger.info(f"白名单条目已更新: id={entry_id} by={operator.uid}")
    return Response()


@router.post("/whitelist/delete/{entry_id}", response_model=Response)
async def whitelist_delete(
    entry_id: int,
    operator: UserInfo = Depends(require_permission("model:edit")),
):
    """删除条目"""
    if not await get_whitelist_entry(entry_id):
        raise NotFoundError("白名单条目不存在")

    await delete_whitelist_entry(entry_id)
    logger.info(f"白名单条目已删除: id={entry_id} by={operator.uid}")
    return Response()


# ==================== 批量授权与视图（2026-10-19）====================


@router.post("/whitelist/batch-create", response_model=Response)
async def whitelist_batch_create(
    body: WhitelistBatchCreate,
    operator: UserInfo = Depends(require_permission("model:edit")),
):
    """多个模型 × 多个部门/角色/成员一次授权

    已存在的（同对象同模型）一律跳过，**不改它的允许/拒绝**，在 skipped 里原样回报。
    """
    depts, users, rules = await load_access_snapshot()
    dept_ids = {d.id for d in depts}
    user_uids = {u.uid for u in users}
    model_ids = {m.id for m, _ in await list_models()}

    missing_models = sorted(set(body.model_ids) - model_ids)
    if missing_models:
        raise NotFoundError(f"模型不存在：{missing_models}")

    targets = []
    for t in body.targets:
        target = GrantTarget(t.scope, t.scope_value).normalized()
        if target.scope == SCOPE_DEPT and (
            not target.scope_value.isdigit() or int(target.scope_value) not in dept_ids
        ):
            raise NotFoundError(f"部门不存在：{target.scope_value}")
        if target.scope == SCOPE_ROLE and target.scope_value not in ALL_ROLES:
            raise ValidationError_(f"角色非法：{target.scope_value}")
        if target.scope == SCOPE_USER and target.scope_value not in user_uids:
            raise NotFoundError(f"用户不存在：{target.scope_value}")
        targets.append(target)

    plan = plan_batch_grants(body.model_ids, targets, [WhitelistRule.from_orm(r) for r in rules])
    if len(plan.to_create) > MAX_BATCH_ROWS:
        raise ValidationError_(
            f"一次最多生成 {MAX_BATCH_ROWS} 条授权，本次 {len(plan.to_create)} 条；"
            "人数多时请改用部门或角色授权"
        )

    try:
        created = await insert_whitelist_entries(
            (t.scope, t.scope_value, model_id, body.allowed, body.remark)
            for t, model_id in plan.to_create
        )
    except IntegrityError:
        # 与别人同时在配同一批：整批回滚，让操作者刷新后重试，而不是留半截
        raise ConflictError("部分条目刚被其他人创建，本次未写入，请刷新后重试")

    logger.info(
        f"白名单批量授权: models={sorted(set(body.model_ids))} targets={len(targets)} "
        f"allowed={body.allowed} created={created} skipped={len(plan.skipped)} by={operator.uid}"
    )
    return Response(data={
        "created": created,
        "skipped": [
            {"scope": t.scope, "scope_value": t.scope_value, "model_id": m, "existing_allowed": a}
            for t, m, a in plan.skipped
        ],
    })


@router.post("/whitelist/batch-delete", response_model=Response)
async def whitelist_batch_delete(
    body: WhitelistBatchDelete,
    operator: UserInfo = Depends(require_permission("model:edit")),
):
    """批量删除条目"""
    deleted = await delete_whitelist_entries(body.ids)
    logger.info(f"白名单批量删除: ids={sorted(set(body.ids))} deleted={deleted} by={operator.uid}")
    return Response(data={"deleted": deleted})


def _snapshot_refs(depts, users):
    nodes = {d.id: DeptNode(d.id, d.name, d.parent_id) for d in depts}
    members = [
        MemberRef(
            uid=u.uid,
            name=u.realname or u.nickname or u.username,
            role=u.permission,
            dept_id=u.dept_id,
            status=1 if u.status == 1 else 0,
        )
        for u in users
        if u.uid
    ]
    return nodes, members


@router.get("/whitelist/overview", response_model=Response)
async def whitelist_overview(
    _: UserInfo = Depends(require_permission("model:view")),
):
    """按模型看授权：开放给了哪些部门（含下级数）/角色/成员、被谁拒绝、实际多少在职成员能用"""
    depts, users, rules = await load_access_snapshot()
    nodes, members = _snapshot_refs(depts, users)
    models = [m for m, _ in await list_models()]
    overview = model_grant_overview(
        [m.id for m in models],
        [WhitelistRule.from_orm(r) for r in rules],
        nodes,
        members,
    )
    by_id = {m.id: m for m in models}
    for item in overview:
        model = by_id[item["model_id"]]
        item["display_name"] = model.display_name
        item["public_name"] = model_public_name(model)
        item["status"] = model.status
    return Response(data={"items": overview, "active_user_count": sum(m.status == 1 for m in members)})


@router.get("/whitelist/member-access/{uid}", response_model=Response)
async def whitelist_member_access(
    uid: str,
    _: UserInfo = Depends(require_permission("model:view")),
):
    """按成员反查：每个相关模型能不能用、来自哪条规则（标出继承自哪个上级部门）

    结论与 /v1/models 同一套判定；但这里**不剔除**停用模型或停用渠道，便于看清
    「授权了但模型关着」的情况，前端按 model_status 标出来。
    """
    depts, users, rules = await load_access_snapshot()
    nodes, members = _snapshot_refs(depts, users)
    member = next((m for m in members if m.uid == uid), None)
    if member is None:
        raise NotFoundError("用户不存在")

    models = {m.id: m for m, _ in await list_models()}
    report = member_access_report(member, [WhitelistRule.from_orm(r) for r in rules], nodes, members)
    for item in report:
        model = models.get(item["model_id"])
        item["display_name"] = model.display_name if model else f"#{item['model_id']}"
        item["public_name"] = model_public_name(model) if model else None
        item["model_status"] = model.status if model else None
    return Response(data={
        "member": {
            "uid": member.uid,
            "name": member.name,
            "role": member.role,
            "status": member.status,
            "dept_id": member.dept_id,
            "dept_path": dept_path(member.dept_id, nodes),
        },
        "items": report,
    })
