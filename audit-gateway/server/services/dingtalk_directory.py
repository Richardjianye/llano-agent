"""钉钉内部应用通讯录适配器；只保留用于核对绑定的姓名、部门、稳定身份。"""

import asyncio
import re
from dataclasses import dataclass, field

import httpx
from config import get_settings
from services.dingtalk_identity import (LEGACY_API_URL, DingTalkAuthError,
                                       _app_access_token, _json)


@dataclass
class DirectoryRoster:
    provider: str
    tenant_id: str
    members: list[dict] = field(default_factory=list)
    departments: list[dict] = field(default_factory=list)
    scope: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    complete: bool = False


async def fetch_roster(settings=None) -> DirectoryRoster:
    cfg = settings or get_settings()
    if not all((cfg.dingtalk_client_id, cfg.dingtalk_client_secret, cfg.dingtalk_corp_id)):
        raise DingTalkAuthError("钉钉通讯录尚未配置")
    roster = DirectoryRoster("dingtalk", cfg.dingtalk_corp_id)
    members: dict[str, dict] = {}
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            token = await _app_access_token(client, cfg)

            async def call(path, body=None):
                # 限制串行调用速率；权限错误和畸形响应不能被当成空列表。
                await asyncio.sleep(0.05)
                response = (await client.get(LEGACY_API_URL + path, params={"access_token": token})
                            if body is None else await client.post(
                                LEGACY_API_URL + path, params={"access_token": token}, json=body))
                data = await _json(response)
                if str(data.get("errcode")) != "0":
                    code = data.get("errcode")
                    scopes = re.findall(r"qyapi_[a-z_]+", str(data.get("errmsg", "")))
                    needed = "；缺少 " + ", ".join(sorted(set(scopes))) if scopes else ""
                    raise DingTalkAuthError(f"通讯录接口 {path} 未通过（错误码 {code}{needed}），请检查权限和通讯录范围")
                return data

            scopes = await call("/auth/scopes")
            scope = scopes.get("auth_org_scopes")
            if not isinstance(scope, dict) or not isinstance(scope.get("authed_dept"), list):
                raise DingTalkAuthError("未取得可核对的通讯录授权范围")
            # 根部门 1 在授权范围内才视为覆盖整个企业；局部范围仍可核对可见员工。
            roster.scope = {"departments": [str(x) for x in scope["authed_dept"]],
                            "explicit_user_count": len(scope.get("authed_user", []))}
            full_scope = "1" in roster.scope["departments"]
            queue = [1]
            visited = set()
            roster.departments = [{"id": "1", "name": "企业根部门", "parent_id": None}]
            while queue:
                dept_id = queue.pop(0)
                if dept_id in visited or len(visited) >= 5000:
                    raise DingTalkAuthError("部门树重复或超出同步上限")
                visited.add(dept_id)
                children = (await call("/topapi/v2/department/listsub", {"dept_id": dept_id})).get("result")
                if not isinstance(children, list):
                    raise DingTalkAuthError("部门列表响应无效")
                for dept in children:
                    child = dept.get("dept_id")
                    if not isinstance(child, int) or child <= 0 or child in visited or child in queue:
                        raise DingTalkAuthError("部门树包含无效或重复标识")
                    queue.append(child)
                    roster.departments.append({"id": str(child), "name": str(dept.get("name", "")),
                                               "parent_id": str(dept_id)})
                cursor, seen_cursors = 0, set()
                while True:
                    if cursor in seen_cursors or len(seen_cursors) >= 1000:
                        raise DingTalkAuthError("成员分页重复或超出同步上限")
                    seen_cursors.add(cursor)
                    result = (await call("/topapi/v2/user/list", {
                        "dept_id": dept_id, "cursor": cursor, "size": 100,
                        "order_field": "custom", "contain_access_limit": True,
                    })).get("result")
                    if (not isinstance(result, dict) or not isinstance(result.get("list"), list)
                            or not isinstance(result.get("has_more"), bool)):
                        raise DingTalkAuthError("成员分页响应无效")
                    for raw in result["list"]:
                        user_id, union_id = raw.get("userid"), raw.get("unionid")
                        if not isinstance(user_id, str) or not user_id:
                            raise DingTalkAuthError("成员缺少企业内标识")
                        member = {"user_id": user_id, "subject_id": union_id or "",
                                  "name": str(raw.get("name", "")),
                                  "dept_ids": [str(x) for x in raw.get("dept_id_list", [dept_id])]}
                        old = members.get(user_id)
                        if old and (old["subject_id"] != member["subject_id"] or old["name"] != member["name"]):
                            raise DingTalkAuthError("同一成员跨部门返回的身份不一致，请重新同步")
                        if old:
                            member["dept_ids"] = sorted(set(old["dept_ids"] + member["dept_ids"]))
                        members[user_id] = member
                    if not result["has_more"]:
                        break
                    cursor = result.get("next_cursor")
                    if not isinstance(cursor, int) or cursor < 0:
                        raise DingTalkAuthError("成员分页缺少下一页游标")
            roster.complete = full_scope
            if not full_scope:
                roster.errors.append("通讯录范围未覆盖企业根部门，本次为部分快照；不能据此判断离职")
    except (DingTalkAuthError, httpx.RequestError) as exc:
        roster.errors.append(str(exc) if isinstance(exc, DingTalkAuthError) else "通讯录网络请求失败，请重试")
    roster.members = list(members.values())
    return roster


# 企业微信/飞书以后各自实现同一 fetch_roster 返回契约。
DIRECTORY_PROVIDERS = {"dingtalk": fetch_roster}
