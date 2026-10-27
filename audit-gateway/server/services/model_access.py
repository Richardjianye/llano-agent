"""模型访问控制 —— 按部门/角色/用户解析可用模型（TODO 决策 #6、#7）

判定规则（顺序即优先级）：
1. 收集 global / dept / role / user 四个作用域下命中该用户的全部规则。
   **dept 规则对本部门及其所有上级部门生效**（2026-10-19）：视觉中心开放的模型，
   视觉中心下面设计创意部的人也能用。方向只向上继承，不向下、不横向 ——
   给设计创意部开的模型，视觉中心本级的人用不了。
2. 允许集合 = 四类 allowed=True 规则的并集
3. 任一 allowed=False 的规则命中，则该模型被显式拒绝，无论有多少允许规则。
   拒绝同样沿部门链向下继承：上级部门显式拒绝，下级部门、角色、个人授权都救不回来
4. 允许集合为空 => 该用户没有任何可用模型（默认拒绝，不是默认放行）

第 3、4 条是刻意的：白名单要"默认拒绝"，加错一条允许规则的代价远小于漏掉一条拒绝规则。
「下级/个人例外放开上级的拒绝」这种最具体者优先的语义**没有做**：那样一条个人授权就能
绕过部门禁令，审计时得逐层推演才看得出谁能用。要给某个下级开，就别在上级拒绝，改为
只给需要的部门授权。

为什么之前不继承：dept 规则只比 `dept_id == scope_value`，一个等号。2026-10 生产上
源贺（设计创意部，上级视觉中心）因此用不了视觉中心开放的 GPT，管理员只好给他单独
加个人授权。部门树的方向约定见 services/org_tree.py。

纯函数部分不碰数据库，便于单测。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from loguru import logger
from model.database import (SCOPE_DEPT, SCOPE_GLOBAL, SCOPE_ROLE, SCOPE_USER,
                            ModelInfo, ModelWhitelist, UpstreamProvider)


@dataclass(frozen=True)
class WhitelistRule:
    """一条白名单规则（与 ORM 解耦，方便测试构造）"""

    scope: str
    scope_value: str
    model_id: int
    allowed: bool = True

    @classmethod
    def from_orm(cls, row: ModelWhitelist) -> "WhitelistRule":
        return cls(
            scope=row.scope,
            scope_value=row.scope_value or "",
            model_id=row.model_id,
            allowed=bool(row.allowed),
        )


@dataclass(frozen=True)
class AccessSubject:
    """做权限判定所需的用户最小信息"""

    uid: str
    role: str
    dept_id: Optional[int] = None
    # 本部门 + 所有上级部门（见 org_tree.expand_ancestors）。为空时只按 dept_id 本身
    # 匹配 —— 少继承是「看不到」，不会多给。数据库侧入口会先用 with_dept_chain 补齐。
    dept_chain: Tuple[int, ...] = ()

    def dept_ids(self) -> Tuple[int, ...]:
        """参与 dept 规则匹配的部门集合"""
        if self.dept_id is None:
            return ()
        if self.dept_chain:
            return self.dept_chain
        return (int(self.dept_id),)


def _rule_matches(rule: WhitelistRule, subject: AccessSubject) -> bool:
    """判断一条规则是否命中该用户"""
    if rule.scope == SCOPE_GLOBAL:
        return True
    if rule.scope == SCOPE_DEPT:
        return rule.scope_value in {str(d) for d in subject.dept_ids()}
    if rule.scope == SCOPE_ROLE:
        return rule.scope_value == subject.role
    if rule.scope == SCOPE_USER:
        return rule.scope_value == subject.uid
    # 未知作用域一律不生效，避免脏数据意外放行
    return False


def resolve_allowed_model_ids(
    rules: Iterable[WhitelistRule], subject: AccessSubject
) -> Set[int]:
    """解析用户最终可用的模型ID集合"""
    allowed: Set[int] = set()
    denied: Set[int] = set()

    for rule in rules:
        if not _rule_matches(rule, subject):
            continue
        if rule.allowed:
            allowed.add(rule.model_id)
        else:
            denied.add(rule.model_id)

    return allowed - denied


def is_model_allowed(
    rules: Iterable[WhitelistRule], subject: AccessSubject, model_id: int
) -> bool:
    """单个模型的准入判定"""
    return model_id in resolve_allowed_model_ids(rules, subject)


@dataclass(frozen=True)
class ModelAccessExplanation:
    """某个模型对某个人「能不能用、为什么」—— 后台反查用

    allow_sources / deny_sources 都是命中的原始规则；dept 规则命中的是上级部门时，
    `inherited_dept_ids` 里会有它，前端据此标「继承自 XX」。
    """

    model_id: int
    allowed: bool
    allow_sources: Tuple[WhitelistRule, ...]
    deny_sources: Tuple[WhitelistRule, ...]


def explain_model_access(
    rules: Iterable[WhitelistRule], subject: AccessSubject
) -> Dict[int, ModelAccessExplanation]:
    """按模型汇总命中的规则。与 resolve_allowed_model_ids 同一套匹配，结论必须一致。"""
    allows: Dict[int, List[WhitelistRule]] = {}
    denies: Dict[int, List[WhitelistRule]] = {}
    for rule in rules:
        if not _rule_matches(rule, subject):
            continue
        bucket = allows if rule.allowed else denies
        bucket.setdefault(rule.model_id, []).append(rule)

    out: Dict[int, ModelAccessExplanation] = {}
    for model_id in set(allows) | set(denies):
        out[model_id] = ModelAccessExplanation(
            model_id=model_id,
            allowed=model_id in allows and model_id not in denies,
            allow_sources=tuple(allows.get(model_id, ())),
            deny_sources=tuple(denies.get(model_id, ())),
        )
    return out


def inherited_dept_id(rule: WhitelistRule, subject: AccessSubject) -> Optional[int]:
    """dept 规则命中的是上级部门时返回那个部门 ID；本部门或非 dept 规则返回 None"""
    if rule.scope != SCOPE_DEPT or subject.dept_id is None:
        return None
    if rule.scope_value == str(subject.dept_id) or not rule.scope_value.isdigit():
        return None
    return int(rule.scope_value)


# ==================== 数据库侧 ====================


async def with_dept_chain(subject: AccessSubject) -> AccessSubject:
    """补齐部门继承链。已经带链（或没有部门）的原样返回。"""
    if subject.dept_id is None or subject.dept_chain:
        return subject
    from dataclasses import replace

    from services.org_tree import dept_ancestor_ids

    chain = await dept_ancestor_ids(subject.dept_id)
    return replace(subject, dept_chain=tuple(chain) or (int(subject.dept_id),))


async def load_rules_for_subject(subject: AccessSubject) -> List[WhitelistRule]:
    """只捞可能命中该用户的规则，避免全表扫描"""
    from sqlalchemy import and_, or_, select

    from db import init_async_session

    subject = await with_dept_chain(subject)

    conditions = [ModelWhitelist.scope == SCOPE_GLOBAL]
    conditions.append(
        and_(ModelWhitelist.scope == SCOPE_ROLE,
             ModelWhitelist.scope_value == subject.role)
    )
    conditions.append(
        and_(ModelWhitelist.scope == SCOPE_USER,
             ModelWhitelist.scope_value == subject.uid)
    )
    if subject.dept_ids():
        conditions.append(
            and_(
                ModelWhitelist.scope == SCOPE_DEPT,
                ModelWhitelist.scope_value.in_([str(d) for d in subject.dept_ids()]),
            )
        )

    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (
            await session.execute(select(ModelWhitelist).where(or_(*conditions)))
        ).scalars().all()

    return [WhitelistRule.from_orm(row) for row in rows]


async def list_available_models(subject: AccessSubject) -> List[ModelInfo]:
    """返回该用户可用且上游可用的模型（模型停用或供应商停用都会被剔除）"""
    from sqlalchemy import select

    from db import init_async_session

    subject = await with_dept_chain(subject)
    rules = await load_rules_for_subject(subject)
    model_ids = resolve_allowed_model_ids(rules, subject)
    if not model_ids:
        return []

    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(ModelInfo)
                .join(UpstreamProvider, UpstreamProvider.id == ModelInfo.provider_id)
                .where(
                    ModelInfo.id.in_(model_ids),
                    ModelInfo.status == 1,
                    UpstreamProvider.status == 1,
                )
                .order_by(ModelInfo.id)
            )
        ).scalars().all()

    return filter_unambiguous_public_models(rows)


def filter_unambiguous_public_models(models: Sequence[ModelInfo]) -> List[ModelInfo]:
    """剔除公开名冲突的模型，避免同名时静默路由到错误渠道

    models 表只保证同一 provider 内 model_name 唯一。两个渠道都提供同名模型且
    都没配 alias 时，对外 ID 会冲突；安全做法是两边都不下发，等管理员给它们
    配置不同别名，而不是按数据库 ID 猜一个。
    """
    counts = Counter(model_public_name(model) for model in models)
    ambiguous = {name for name, count in counts.items() if count > 1}
    if ambiguous:
        logger.warning(f"检测到模型公开名冲突，已拒绝下发: {sorted(ambiguous)}")
    return [model for model in models if counts[model_public_name(model)] == 1]


async def check_model_permission(subject: AccessSubject, model_ref: str) -> Optional[ModelInfo]:
    """按客户端传来的模型名/别名做准入校验，通过则返回模型，否则 None

    转发层接到 /v1/chat/completions 时调用；返回 None 一律当作 10008 无权限处理。
    """
    models = await list_available_models(subject)
    for model in models:
        if model_ref in (model.alias, model.model_name):
            return model
    return None


def subject_from_user(user) -> AccessSubject:
    """从 UserInfo / User ORM 对象构造判定主体"""
    return AccessSubject(
        uid=user.uid,
        role=user.permission,
        dept_id=getattr(user, "dept_id", None),
    )


def model_public_name(model: ModelInfo) -> str:
    """对外暴露的模型名：优先别名，避免上游换型影响客户端"""
    return model.alias or model.model_name


def models_to_payload(models: Sequence[ModelInfo]) -> List[dict]:
    """整理成客户端模型列表结构"""
    from services.image_generation import is_image_model
    return [
        {
            "id": model_public_name(m),
            "display_name": m.display_name,
            "supports_stream": bool(m.supports_stream),
            "supports_multimodal": bool(m.supports_multimodal),
            "max_context_tokens": m.max_context_tokens,
        }
        for m in models
        if not is_image_model(m)
    ]
