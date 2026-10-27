"""部门树 —— 「本部门」到底包不包括下级（2026-09-02）

## 这个模块是一次真实事故的产物

63 个部门里 48 个填了 `parent_id`，树在数据里是完整的。但**没有任何一处权限
判定读过它** —— 所有数据范围都是 `dept_id == X`，一个等号，不向下递归。

在真库上量出来的后果：

    负责人  部门            本部门  下级   看得到 / 实际管辖
    玖九   客户服务中心I        1    47    1 / 48
    白术   国内营销中心         2    32    2 / 34
    子龙   视觉中心           2    29    2 / 31

37 个部门负责人里 8 个受影响，**153 个人对他们的负责人完全不可见**。

玖九打开用量报表看到「1 个人」，他不会觉得这是权限设计，他会觉得系统坏了。

## 两种业务，两条方向明确的函数

「本部门」= 自己 + 所有后代。**不含上级，也不含平级** —— 那是横向越权，
比看不到更糟，对应 `expand_descendants`。

技能空间和配额反过来：子部门继承上级发布的技能，子部门用量也受上级额度约束，
对应 `expand_ancestors`。两个方向分开命名，调用方不能拿同一个含糊的「部门范围」
猜方向。

## 为什么是纯函数 + 一次全表读

部门只有 63 行，整棵树一次读进来在内存里走，比递归 CTE 简单、比缓存安全 ——
缓存的失效点在组织架构调整那一刻，而那正是最不该出错的时刻（人换了部门，
旧负责人还看得到）。真到了几千个部门再谈优化。

拆成纯函数是为了能在**没有数据库**的情况下把环、自环、断链这些脏数据形状
直接测出来。
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional, Tuple

from loguru import logger


def expand_descendants(root: int, parents: Dict[int, Optional[int]]) -> Tuple[int, ...]:
    """返回 `root` 及其所有后代的部门 ID，升序。

    `parents` 是 {部门ID: 上级ID}，顶级的上级为 None。

    ## 脏数据一律按「断在这里」处理，不抛异常

    环（A 的上级是 B，B 的上级是 A）、自环、指向不存在的部门 —— 这些都可能出现，
    而权限判定不能因为一行脏数据整个挂掉。碰到环就停止展开：结果是**少看到**
    一些部门，而不是多看到。方向选错的代价不对称。
    """
    children: Dict[int, list] = {}
    for child, parent in parents.items():
        if parent is None or parent == child:
            continue
        children.setdefault(int(parent), []).append(int(child))

    out = {int(root)}
    frontier = [int(root)]
    while frontier:
        current = frontier.pop()
        for child in children.get(current, ()):
            if child in out:
                # 环。停在这里，并留一行日志 —— 数据该修，但不该由权限判定来报错
                logger.warning(f"部门树疑似成环：{current} -> {child}，展开到此为止")
                continue
            out.add(child)
            frontier.append(child)

    return tuple(sorted(out))


def expand_ancestors(root: int, parents: Dict[int, Optional[int]]) -> Tuple[int, ...]:
    """返回 `root` 及其所有上级部门 ID，升序。"""
    root = int(root)
    out = {root}
    current = root

    while True:
        parent = parents.get(current)
        if parent is None or int(parent) == current:
            break

        parent = int(parent)
        if parent in out:
            logger.warning(f"部门树疑似成环：{current} -> {parent}，向上展开到此为止")
            return (root,)
        if parent not in parents:
            logger.warning(f"部门树指向不存在的上级：{current} -> {parent}，向上展开到此为止")
            break

        out.add(parent)
        current = parent

    return tuple(sorted(out))


async def dept_scope_ids(dept_id: Optional[int]) -> Tuple[int, ...]:
    """某个部门负责人的**数据范围**：本部门 + 所有下级。

    `dept_id` 为空时返回空元组 —— 调用方必须把它当成「没有部门范围」，
    **不能当成「不加限制」**。这两者差一个公司。
    """
    if dept_id is None:
        return ()

    from db import init_async_session
    from model.database import Department
    from sqlalchemy import select

    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (await session.execute(select(Department.id, Department.parent_id))).all()

    return expand_descendants(int(dept_id), {int(r[0]): r[1] for r in rows})


async def dept_ancestor_ids(dept_id: Optional[int]) -> Tuple[int, ...]:
    """某个部门的继承链：本部门 + 所有上级。无部门时返回空元组。"""
    if dept_id is None:
        return ()

    from db import init_async_session
    from model.database import Department
    from sqlalchemy import select

    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (await session.execute(select(Department.id, Department.parent_id))).all()

    return expand_ancestors(int(dept_id), {int(r[0]): r[1] for r in rows})


def as_filter(ids: Iterable[int]) -> Tuple[int, ...]:
    """把展开结果收成一个稳定的元组，方便直接喂给 `in_()` 与断言。"""
    return tuple(sorted({int(i) for i in ids}))
