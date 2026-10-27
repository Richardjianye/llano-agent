"""全员通讯录 —— 客户端把人拉进项目时用来找人（2026-09-07）

## 为什么开放给全员

在这之前，`/v1/projects/{code}/members` 要一个 `user_uid`，而客户端**没有任何
办法拿到别人的 uid** —— 后台的账号列表要 `user:view`，普通员工没有。结果是
「协作全部在客户端进行」这句话缺了最后一块：项目建得出来、自己的 agent 拉得
进来，但没法把同事拉进来。

开放范围由负责人拍板：与钉钉一致，通讯录全员可见。

## 但通讯录不等于账号列表

这里**不复用** `/admin/account/list`，也不复用它底下的 `list_users`：

- 只回**在职**账号。停用的人不该还能被拉进项目
- 不回角色、不回权限、不回部门 id。「谁是审计员」不是全员该知道的事，而
  「谁在哪个部门」用部门**名称**答就够了，暴露 id 只方便了想遍历的人
- 不回任何联系方式。这里要回答的问题只有一个：「把谁加进来」

多一个字段的代价不是这一次多显示一点，是它会被下一个页面直接拿去用。

## 群聊令牌拿不到通讯录

作用域不是 private 的凭据一律拒。群聊 turn 里的 agent 正在读别人的消息，让它
能把全员名单拉一遍，等于把「能力不跨界」（决策 #45）开了一个只出不进的口子 ——
名单出去了就收不回来，而它连一次工具调用都不需要。
"""

from fastapi import APIRouter, Depends, Query, Response

import services.conversation_scope as scope_service
from db.selects.department import list_departments
from db.selects.user import count_directory, search_directory
from utils.errors import AppError
from utils.gateway_auth import Caller, require_caller

router = APIRouter()


@router.get("/directory")
async def directory(
    response: Response,
    q: str = Query("", max_length=64, description="按用户名或姓名模糊匹配"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    caller: Caller = Depends(require_caller),
):
    """在职同事列表。字段刻意只有四个，见模块说明。"""
    # 与其他作用域拒绝同一句话：不告诉对方「你手里这把是群聊令牌」
    if scope_service.scope_from_credential(caller) != scope_service.PRIVATE:
        raise AppError(code=10006, message=scope_service.SCOPE_DENIED_MESSAGE,
                       http_status=403)

    response.headers["Cache-Control"] = "no-store"

    keyword = (q or "").strip()
    users = await search_directory(keyword=keyword, limit=limit, offset=offset)
    # 部门表很小（15 行），一次查完在内存里对，比给每个用户发一次查询好。
    dept_names = {d.id: d.name for d in await list_departments(include_disabled=True)}

    return {
        "users": [
            {
                "uid": u.uid,
                "username": u.username,
                "realname": u.realname or "",
                "dept_name": dept_names.get(u.dept_id, ""),
            }
            for u in users
        ],
        "total": await count_directory(keyword=keyword),
    }
