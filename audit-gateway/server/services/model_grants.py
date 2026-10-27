"""白名单的批量配置与「一目了然」视图（2026-10-19）

白名单本身的判定在 services/model_access.py，这里只做两件管理侧的事：

1. **批量授权**：一次选多个模型 × 多个部门/角色/成员，算出要新建哪些条目、哪些
   已经存在。已存在的一律跳过、原样回报，**不覆盖** —— 同一对象同一模型已经是
   「显式拒绝」时，批量「允许」把它悄悄翻过来，是最难事后追查的那种改动。
2. **按模型看授权 / 按成员反查**：某个模型开放给了哪些部门（含多少下级部门）、
   角色、成员，实际有多少在职成员能用；某个成员能用哪些模型、每个来自哪条规则。
   有效人数与反查都调 model_access 的同一套匹配，不另写一份判定。

全部是纯函数，数据库读取由路由层一次性准备好再传进来（部门 60 多行、成员几百人、
白名单几十条，全量在内存里算比拼 SQL 简单，也不会和真实判定口径分叉）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from model.database import SCOPE_DEPT, SCOPE_GLOBAL, SCOPE_ROLE, SCOPE_USER
from services.model_access import (AccessSubject, WhitelistRule,
                                   explain_model_access, inherited_dept_id,
                                   resolve_allowed_model_ids)
from services.org_tree import expand_ancestors, expand_descendants

# 一次批量最多生成的条目数。超过多半是选错了（比如把全员逐个选上），
# 应该改用部门或角色授权。
MAX_BATCH_ROWS = 2000


@dataclass(frozen=True)
class GrantTarget:
    scope: str
    scope_value: str = ""

    def normalized(self) -> "GrantTarget":
        if self.scope == SCOPE_GLOBAL:
            return GrantTarget(SCOPE_GLOBAL, "")
        return GrantTarget(self.scope, str(self.scope_value or "").strip())


@dataclass(frozen=True)
class BatchPlan:
    to_create: Tuple[Tuple[GrantTarget, int], ...]
    # (对象, 模型ID, 已有条目是否为允许) —— 前端据此提示「其中 N 条已是显式拒绝，未改动」
    skipped: Tuple[Tuple[GrantTarget, int, bool], ...]


def plan_batch_grants(
    model_ids: Iterable[int],
    targets: Iterable[GrantTarget],
    existing: Iterable[WhitelistRule],
) -> BatchPlan:
    """模型 × 对象做笛卡尔积，去重，扣掉已有条目"""
    existing_map: Dict[Tuple[str, str, int], bool] = {
        (r.scope, r.scope_value, r.model_id): r.allowed for r in existing
    }
    seen: Set[Tuple[str, str, int]] = set()
    create: List[Tuple[GrantTarget, int]] = []
    skipped: List[Tuple[GrantTarget, int, bool]] = []

    unique_targets: List[GrantTarget] = []
    for target in targets:
        target = target.normalized()
        if target not in unique_targets:
            unique_targets.append(target)

    for model_id in dict.fromkeys(int(m) for m in model_ids):
        for target in unique_targets:
            key = (target.scope, target.scope_value, model_id)
            if key in seen:
                continue
            seen.add(key)
            if key in existing_map:
                skipped.append((target, model_id, existing_map[key]))
            else:
                create.append((target, model_id))

    return BatchPlan(tuple(create), tuple(skipped))


# ==================== 视图 ====================


@dataclass(frozen=True)
class DeptNode:
    id: int
    name: str
    parent_id: Optional[int]


@dataclass(frozen=True)
class MemberRef:
    uid: str
    name: str
    role: str
    dept_id: Optional[int]
    status: int = 1


def dept_path(dept_id: Optional[int], depts: Dict[int, DeptNode]) -> str:
    """「视觉中心 / 设计创意部」；部门不存在时回落成 #ID"""
    if dept_id is None:
        return ""
    if int(dept_id) not in depts:
        return f"#{dept_id}"
    # 自顶向下排；碰到环或断链就停（与 org_tree 的脏数据约定一致）
    ordered: List[int] = []
    current: Optional[int] = int(dept_id)
    while current is not None and current in depts and current not in ordered:
        ordered.append(current)
        current = depts[current].parent_id
    return " / ".join(depts[i].name for i in reversed(ordered))


def subject_of(member: MemberRef, depts: Dict[int, DeptNode]) -> AccessSubject:
    parents = {d.id: d.parent_id for d in depts.values()}
    chain: Tuple[int, ...] = ()
    if member.dept_id is not None:
        chain = expand_ancestors(int(member.dept_id), parents)
    return AccessSubject(uid=member.uid, role=member.role,
                         dept_id=member.dept_id, dept_chain=chain)


def _scope_label(rule: WhitelistRule, depts: Dict[int, DeptNode],
                 members: Dict[str, MemberRef], role_label) -> str:
    if rule.scope == SCOPE_GLOBAL:
        return "全员"
    if rule.scope == SCOPE_DEPT:
        if rule.scope_value.isdigit():
            return dept_path(int(rule.scope_value), depts) or f"#{rule.scope_value}"
        return rule.scope_value
    if rule.scope == SCOPE_ROLE:
        return role_label(rule.scope_value)
    if rule.scope == SCOPE_USER:
        member = members.get(rule.scope_value)
        return member.name if member else rule.scope_value
    return f"{rule.scope}:{rule.scope_value}"


def model_grant_overview(
    model_ids: Sequence[int],
    rules: Sequence[WhitelistRule],
    depts: Dict[int, DeptNode],
    members: Sequence[MemberRef],
    role_label=lambda r: r,
) -> List[dict]:
    """每个模型开放给了谁、拒绝了谁、实际多少在职成员能用"""
    parents = {d.id: d.parent_id for d in depts.values()}
    member_map = {m.uid: m for m in members}
    active = [m for m in members if m.status == 1]
    subjects = [(m, subject_of(m, depts)) for m in active]
    allowed_by_member = {m.uid: resolve_allowed_model_ids(rules, s) for m, s in subjects}

    out = []
    for model_id in model_ids:
        model_rules = [r for r in rules if r.model_id == model_id]
        grants: Dict[str, List[dict]] = {"global": [], "dept": [], "role": [], "user": []}
        for rule in model_rules:
            item = {
                "scope_value": rule.scope_value,
                "label": _scope_label(rule, depts, member_map, role_label),
                "allowed": rule.allowed,
            }
            if rule.scope == SCOPE_DEPT and rule.scope_value.isdigit() and int(rule.scope_value) in parents:
                # 继承是向下的：给这个部门开，等于给它的全部下级部门开
                item["descendant_dept_count"] = len(expand_descendants(int(rule.scope_value), parents)) - 1
            grants.setdefault(rule.scope, []).append(item)
        effective = [m.uid for m in active if model_id in allowed_by_member[m.uid]]
        out.append({
            "model_id": model_id,
            "grants": grants,
            "rule_count": len(model_rules),
            "effective_user_count": len(effective),
        })
    return out


def member_access_report(
    member: MemberRef,
    rules: Sequence[WhitelistRule],
    depts: Dict[int, DeptNode],
    members: Sequence[MemberRef] = (),
    role_label=lambda r: r,
) -> List[dict]:
    """某成员每个相关模型的结论与来源。来源里标出「继承自哪个上级部门」。"""
    subject = subject_of(member, depts)
    member_map = {m.uid: m for m in members}
    member_map.setdefault(member.uid, member)

    def source(rule: WhitelistRule) -> dict:
        inherited = inherited_dept_id(rule, subject)
        return {
            "scope": rule.scope,
            "scope_value": rule.scope_value,
            "label": _scope_label(rule, depts, member_map, role_label),
            "allowed": rule.allowed,
            "inherited_from_dept_id": inherited,
        }

    report = []
    for model_id, explained in sorted(explain_model_access(rules, subject).items()):
        report.append({
            "model_id": model_id,
            "allowed": explained.allowed,
            "allow_sources": [source(r) for r in explained.allow_sources],
            "deny_sources": [source(r) for r in explained.deny_sources],
        })
    return report
