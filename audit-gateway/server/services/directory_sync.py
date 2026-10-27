"""同步预览、人工批量确认与逐行事务；平台目录不会直接改变本地权限。"""
import json
import uuid
from collections import Counter
from datetime import timedelta

from config import get_settings
from db import init_async_session
from db.updates.apikey import revoke_user_api_keys
from loguru import logger
from model.database import (Department, DirectoryOperation, DirectorySnapshot,
                            ExternalIdentity, User, now)
from services.dingtalk_directory import DIRECTORY_PROVIDERS
from services.permissions import can_manage
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from utils.crypt import hash_password_async
from utils.errors import PermissionError_, ValidationError_
from utils.generate import generate_password, generate_user_id
from utils.token_manager import get_token_manager


def make_diff(roster, users, identities, departments, operator, previous=None):
    """姓名只生成候选；无论候选数量多少，都由管理员明确指定 uid。"""
    local = {u.uid: u for u in users if u.uid}
    by_subject = {i.subject_id: i for i in identities}
    by_user = {i.user_uid: i for i in identities}
    member_ids = {m["user_id"] for m in roster["members"]}
    subjects = Counter(m["subject_id"] for m in roster["members"] if m["subject_id"])
    member_counts = Counter(m["user_id"] for m in roster["members"])
    dept_counts = Counter(d.dingtalk_dept_id for d in departments if d.dingtalk_dept_id and d.status == 1)
    dept_map = {d.dingtalk_dept_id: d.id for d in departments
                if d.dingtalk_dept_id and d.status == 1 and dept_counts[d.dingtalk_dept_id] == 1}
    rows = []

    def account(u):
        return {"uid": u.uid, "username": u.username, "name": u.realname or u.username,
                "dept_id": u.dept_id, "status": u.status,
                "manageable": u.uid != operator.uid and can_manage(operator.permission, u.permission),
                "bound": u.uid in by_user, "account_kind": u.account_kind}

    for member in roster["members"]:
        binding = by_subject.get(member["subject_id"])
        target = local.get(binding.user_uid) if binding else None
        candidates = [account(u) for u in users if u.uid and member["name"]
                      and member["name"] in {u.realname, u.username, u.nickname}]
        conflict = (not member["subject_id"] or subjects[member["subject_id"]] != 1
                    or member_counts[member["user_id"]] != 1
                    or (binding is not None and (target is None or binding.provider_user_id != member["user_id"])))
        category = "conflict" if conflict else "bound" if binding else "ambiguous" if len(candidates) > 1 else "candidate" if candidates else "new"
        mapped = sorted({dept_map[d] for d in member["dept_ids"] if d in dept_map})
        rows.append({"key": "member:" + member["user_id"], "category": category,
                     "member": member, "account": account(target) if target else None,
                     "candidates": candidates, "mapped_dept_ids": mapped,
                     "can_bind": not conflict and not binding,
                     "can_create": not conflict and not binding and not candidates,
                     "can_disable": False})
    prior_ids = {m["user_id"] for m in previous["members"]} if previous and previous.get("complete") else set()
    for user in users:
        binding = by_user.get(user.uid)
        if binding and binding.provider_user_id in member_ids:
            continue
        external = user.account_kind == "external"
        # 仅同步前就已绑定、连续两次完整名册缺席的内部员工才有禁用选项。
        absent_twice = bool(not external and binding and roster["complete"] and previous
                            and previous.get("complete") and binding.provider_user_id
                            and binding.provider_user_id not in prior_ids
                            and binding.created_at <= previous["started_at"])
        rows.append({"key": "account:" + str(user.uid),
                     "category": "external" if external else "missing" if binding else "system_only",
                     "account": account(user), "member": None, "candidates": [],
                     "mapped_dept_ids": [], "can_bind": False, "can_create": False,
                     "can_disable": absent_twice and account(user)["manageable"] and user.status == 1})
    return rows


async def _context(session, tenant):
    users = list((await session.execute(select(User).order_by(User.id))).scalars())
    identities = list((await session.execute(select(ExternalIdentity).where(
        ExternalIdentity.provider == "dingtalk", ExternalIdentity.tenant_id == tenant))).scalars())
    departments = list((await session.execute(select(Department))).scalars())
    return users, identities, departments


async def preview(operator):
    factory = await init_async_session()
    tenant = get_settings().dingtalk_corp_id
    if not tenant:
        raise ValidationError_("钉钉通讯录尚未配置")
    snapshot_id = str(uuid.uuid4())
    async with factory() as session:
        # 开始时就落库，失败/同时发起的同步不会被跳过当成连续完整快照。
        snapshot = DirectorySnapshot(id=snapshot_id, provider="dingtalk", tenant_id=tenant,
                                     operator_uid=operator.uid, payload='{}')
        session.add(snapshot)
        await session.commit()
    roster = await DIRECTORY_PROVIDERS["dingtalk"]()
    data = {"provider": roster.provider, "tenant_id": roster.tenant_id,
            "members": roster.members, "departments": roster.departments,
            "scope": roster.scope, "errors": roster.errors, "complete": roster.complete}
    async with factory() as session:
        snapshot = await session.get(DirectorySnapshot, snapshot_id)
        snapshot.payload = json.dumps(data, ensure_ascii=False)
        snapshot.complete = roster.complete
        snapshot.completed_at = now()
        await session.commit()
        return await _preview_payload(session, snapshot, operator)


async def _preview_payload(session, snapshot, operator):
    roster = json.loads(snapshot.payload)
    previous_snapshot = (await session.execute(select(DirectorySnapshot).where(
        DirectorySnapshot.provider == snapshot.provider,
        DirectorySnapshot.tenant_id == snapshot.tenant_id,
        DirectorySnapshot.created_at < snapshot.created_at,
    ).order_by(DirectorySnapshot.created_at.desc()).limit(1))).scalar_one_or_none()
    previous = None
    if previous_snapshot and previous_snapshot.complete and previous_snapshot.completed_at:
        previous = json.loads(previous_snapshot.payload)
        previous["started_at"] = previous_snapshot.created_at
    last_success = (await session.execute(select(DirectorySnapshot.completed_at).where(
        DirectorySnapshot.provider == snapshot.provider,
        DirectorySnapshot.tenant_id == snapshot.tenant_id,
        DirectorySnapshot.complete.is_(True),
    ).order_by(DirectorySnapshot.completed_at.desc()).limit(1))).scalar_one_or_none()
    users, identities, departments = await _context(session, snapshot.tenant_id)
    return {"snapshot_id": snapshot.id, "created_at": snapshot.created_at.isoformat(),
            "complete": roster["complete"], "scope": roster["scope"], "errors": roster["errors"],
            "last_success_at": last_success.isoformat() if last_success else None,
            "departments": roster["departments"],
            "accounts": [{"uid": u.uid, "username": u.username, "name": u.realname or u.username,
                          "dept_id": u.dept_id} for u in users if u.uid and u.status == 1
                         and u.account_kind == "internal" and u.uid != operator.uid
                         and can_manage(operator.permission, u.permission)
                         and not any(i.user_uid == u.uid for i in identities)],
            "rows": make_diff(roster, users, identities, departments, operator, previous)}


async def apply(body, operator):
    if len({a.key for a in body.actions}) != len(body.actions):
        raise ValidationError_("同一记录不能重复选择")
    factory = await init_async_session()
    results, credentials = [], []
    for action in body.actions:
        async with factory() as session:
            # MySQL 行锁串行化同一预览的写入；跨预览最终仍由身份唯一约束拦截。
            snapshot = (await session.execute(select(DirectorySnapshot).where(
                DirectorySnapshot.id == body.snapshot_id).with_for_update())).scalar_one_or_none()
            if (not snapshot or snapshot.operator_uid != operator.uid
                    or snapshot.tenant_id != get_settings().dingtalk_corp_id
                    or not snapshot.completed_at or snapshot.created_at < now() - timedelta(minutes=30)):
                if not results:
                    raise ValidationError_("同步预览已失效或不属于你，请重新同步")
                results.append({"key": action.key, "status": "failed", "message": "同步预览已失效，请重新同步"})
                continue
            latest = (await session.execute(select(DirectorySnapshot.id).where(
                DirectorySnapshot.provider == snapshot.provider,
                DirectorySnapshot.tenant_id == snapshot.tenant_id,
            ).order_by(DirectorySnapshot.created_at.desc()).limit(1))).scalar_one()
            if latest != snapshot.id:
                if not results:
                    raise ValidationError_("已有更新的同步预览，请重新同步")
                results.append({"key": action.key, "status": "failed", "message": "已有更新的同步预览，请重新同步"})
                continue
            done = (await session.execute(select(DirectoryOperation).where(
                DirectoryOperation.snapshot_id == snapshot.id,
                DirectoryOperation.row_key == action.key))).scalar_one_or_none()
            if done:
                results.append({"key": action.key, "status": "already_applied", "uid": done.user_uid})
                continue
            context = await _preview_payload(session, snapshot, operator)
            row = next((r for r in context["rows"] if r["key"] == action.key), None)
            try:
                if row is None or not row.get("can_" + action.action):
                    raise ValidationError_("该记录不能执行所选操作，请核对冲突或同步完整性")
                if action.action == "create":
                    if not action.username:
                        raise ValidationError_("新建账号必须填写用户名")
                    if not can_manage(operator.permission, "employee"):
                        raise PermissionError_("无权创建员工账号")
                    if action.dept_id is not None and action.dept_id not in row["mapped_dept_ids"]:
                        raise ValidationError_("部门尚未映射，请先在部门管理中核对钉钉部门标识")
                    password = generate_password()
                    # CSV 中的密码不能以公式前缀开头，避免表格软件改变复制结果。
                    while password[0] in "=+@-":
                        password = generate_password()
                    target = User(uid=generate_user_id(), username=action.username,
                                  password=await hash_password_async(password),
                                  realname=row["member"]["name"][:36], nickname=action.username,
                                  dept_id=action.dept_id, permission="employee", status=1,
                                  account_kind="internal", must_change_password=True)
                    session.add(target)
                    await session.flush()
                else:
                    uid = action.uid if action.action == "bind" else row["account"]["uid"]
                    target = (await session.execute(select(User).where(User.uid == uid).with_for_update())).scalar_one_or_none()
                    if (not target or target.status != 1 or target.account_kind != "internal"
                            or target.uid == operator.uid or not can_manage(operator.permission, target.permission)):
                        raise PermissionError_("目标账号已禁用、属于企业外或不在你的管理范围")
                if action.action in {"bind", "create"}:
                    member = row["member"]
                    session.add(ExternalIdentity(provider=snapshot.provider, tenant_id=snapshot.tenant_id,
                                                 subject_id=member["subject_id"], user_uid=target.uid,
                                                 provider_user_id=member["user_id"]))
                else:
                    # 先撤销，任何撤销失败都不会写入成功记录；重试幂等。
                    await get_token_manager().revoke_user_tokens(target.uid, reason="account_disabled")
                    await revoke_user_api_keys(target.uid, reason="account_disabled", by=operator.uid)
                    target.status = 0
                session.add(DirectoryOperation(snapshot_id=snapshot.id, row_key=action.key,
                                               action=action.action, user_uid=target.uid, operator_uid=operator.uid))
                await session.commit()
                results.append({"key": action.key, "status": "success", "uid": target.uid})
                if action.action == "create":
                    credentials.append({"username": target.username, "initial_password": password,
                                        "name": target.realname, "uid": target.uid})
                logger.info("通讯录确认: operator={} snapshot={} action={} uid={}",
                            operator.uid, snapshot.id, action.action, target.uid)
            except (ValidationError_, PermissionError_, IntegrityError) as exc:
                await session.rollback()
                message = "用户名或身份发生冲突，请重新核对" if isinstance(exc, IntegrityError) else exc.message
                results.append({"key": action.key, "status": "failed", "message": message})
    # 初始密码只属于本次响应，不写入快照/日志/幂等结果。
    return {"results": results, "credentials": credentials}


async def audit_export(body, operator):
    """记录导出意图；服务端从未保存明文文件，客户端成功下载后清除本次密码。"""
    factory = await init_async_session()
    async with factory() as session:
        snapshot = (await session.execute(select(DirectorySnapshot).where(
            DirectorySnapshot.id == body.snapshot_id).with_for_update())).scalar_one_or_none()
        if not snapshot or snapshot.operator_uid != operator.uid:
            raise PermissionError_("无权导出此批账号")
        created = set((await session.execute(select(DirectoryOperation.user_uid).where(
            DirectoryOperation.snapshot_id == snapshot.id, DirectoryOperation.action == "create",
            DirectoryOperation.operator_uid == operator.uid))).scalars())
        if not set(body.user_uids).issubset(created):
            raise ValidationError_("导出只能包含本批次创建的账号")
        for uid in set(body.user_uids):
            key = "export:" + uid
            existing = (await session.execute(select(DirectoryOperation).where(
                DirectoryOperation.snapshot_id == snapshot.id, DirectoryOperation.row_key == key))).scalar_one_or_none()
            if not existing:
                session.add(DirectoryOperation(snapshot_id=snapshot.id, row_key=key, action="export",
                                               user_uid=uid, operator_uid=operator.uid))
        await session.commit()
    logger.info("初始密码表导出: operator={} snapshot={} count={}", operator.uid, body.snapshot_id, len(body.user_uids))
