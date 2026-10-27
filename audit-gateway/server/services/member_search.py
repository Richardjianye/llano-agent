"""成员搜索 —— 各配置页「选人」控件共用的全量搜索（2026-10-19）

## 为什么单独做

模型白名单选成员原来是前端一次拉 `/admin/account/list?limit=200`，再在下拉框里本地
过滤。生产 290 个账号，按 ID 排在 200 名之后的人**根本不在候选里**，搜不到，也不报错。
API Key、MCP、配额几个页面是同一个写法（200/500 条上限）。

这里改成服务端搜：关键字直接打到 users 全表，分页返回，前端只负责展示。

## 契约（另见 server/MEMBER_SEARCH_API.md）

- 关键字匹配：姓名 / 账号 / 昵称做包含匹配（LIKE，通配符已转义）；uid、钉钉 userId
  做精确匹配。完全相等的排在前面，其余按 ID。
- 过滤：部门（可含全部下级）、角色、状态（默认只要在职）、uid 列表（回显已选值用）。
- 只回选人需要的字段：不含密码、登录时间、钉钉 ID、账号类型。

查询拼装是纯函数，便于不连库测 SQL 形状；执行部分与 db/selects 的写法一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from model.database import Department, User
from sqlalchemy import case, func, or_, select

MAX_LIMIT = 100
MAX_UIDS = 200
STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"
STATUS_ALL = "all"
STATUSES = (STATUS_ACTIVE, STATUS_DISABLED, STATUS_ALL)


@dataclass(frozen=True)
class MemberQuery:
    keyword: str = ""
    dept_ids: Tuple[int, ...] = ()   # 已经按 include_descendants 展开好的部门集合
    roles: Tuple[str, ...] = ()
    status: str = STATUS_ACTIVE
    uids: Tuple[str, ...] = ()
    limit: int = 20
    offset: int = 0

    def clamped(self) -> "MemberQuery":
        from dataclasses import replace

        return replace(
            self,
            keyword=(self.keyword or "").strip()[:64],
            limit=max(1, min(int(self.limit), MAX_LIMIT)),
            offset=max(0, int(self.offset)),
            uids=tuple(dict.fromkeys(u for u in self.uids if u))[:MAX_UIDS],
            status=self.status if self.status in STATUSES else STATUS_ACTIVE,
        )


def escape_like(keyword: str) -> str:
    """把用户输入里的 % _ \\ 当普通字符。不转义的话搜「_」等于列出全员。"""
    return keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _apply_filters(stmt, query: MemberQuery):
    if query.status == STATUS_ACTIVE:
        stmt = stmt.where(User.status == 1)
    elif query.status == STATUS_DISABLED:
        stmt = stmt.where(or_(User.status != 1, User.status.is_(None)))
    if query.dept_ids:
        stmt = stmt.where(User.dept_id.in_(query.dept_ids))
    if query.roles:
        stmt = stmt.where(User.permission.in_(query.roles))
    if query.uids:
        stmt = stmt.where(User.uid.in_(query.uids))
    if query.keyword:
        like = f"%{escape_like(query.keyword)}%"
        stmt = stmt.where(or_(
            User.realname.like(like, escape="\\"),
            User.username.like(like, escape="\\"),
            User.nickname.like(like, escape="\\"),
            User.uid == query.keyword,
            User.dingtalk_user_id == query.keyword,
        ))
    return stmt


def build_search_query(query: MemberQuery):
    query = query.clamped()
    stmt = _apply_filters(select(User), query)
    if query.keyword:
        exact = or_(User.realname == query.keyword, User.username == query.keyword)
        stmt = stmt.order_by(case((exact, 0), else_=1), User.id)
    else:
        stmt = stmt.order_by(User.id)
    return stmt.limit(query.limit).offset(query.offset)


def build_count_query(query: MemberQuery):
    """总数与列表共用同一组过滤条件"""
    return _apply_filters(select(func.count(User.id)), query.clamped())


def member_payload(user, depts: Dict[int, Department]) -> dict:
    from services.model_grants import DeptNode, dept_path

    nodes = {i: DeptNode(i, d.name, d.parent_id) for i, d in depts.items()}
    dept = depts.get(user.dept_id) if user.dept_id is not None else None
    return {
        "uid": user.uid,
        "username": user.username,
        "realname": user.realname,
        "nickname": user.nickname,
        "display_name": user.realname or user.nickname or user.username,
        "role": user.permission,
        "status": 1 if user.status == 1 else 0,
        "dept_id": user.dept_id,
        "dept_name": dept.name if dept else None,
        "dept_path": dept_path(user.dept_id, nodes) if user.dept_id is not None else "",
    }


@dataclass
class MemberPage:
    items: List[dict] = field(default_factory=list)
    total: int = 0
    limit: int = 20
    offset: int = 0

    def as_dict(self) -> dict:
        return {
            "items": self.items,
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
            "has_more": self.offset + len(self.items) < self.total,
        }


async def expand_dept_filter(dept_ids: Iterable[int], include_descendants: bool) -> Tuple[int, ...]:
    from services.org_tree import as_filter, dept_scope_ids

    ids = [int(d) for d in dept_ids]
    if not include_descendants:
        return as_filter(ids)
    expanded: List[int] = []
    for dept_id in ids:
        expanded.extend(await dept_scope_ids(dept_id))
    return as_filter(expanded)


async def search_members(query: MemberQuery) -> MemberPage:
    from db import init_async_session

    query = query.clamped()
    session_maker = await init_async_session()
    async with session_maker() as session:
        users = list((await session.execute(build_search_query(query))).scalars().all())
        total = int((await session.execute(build_count_query(query))).scalar_one())
        depts = {
            d.id: d for d in (await session.execute(select(Department))).scalars().all()
        }
    return MemberPage(
        items=[member_payload(u, depts) for u in users],
        total=total,
        limit=query.limit,
        offset=query.offset,
    )


def parse_csv(value: Optional[str]) -> Tuple[str, ...]:
    if not value:
        return ()
    return tuple(v.strip() for v in value.split(",") if v.strip())


def parse_int_csv(value: Optional[str]) -> Tuple[int, ...]:
    out: List[int] = []
    for v in parse_csv(value):
        if not v.isdigit():
            raise ValueError(f"部门 ID 非法：{v}")
        out.append(int(v))
    return tuple(out)


def roles_valid(roles: Sequence[str], allowed: Sequence[str]) -> bool:
    return all(r in allowed for r in roles)
