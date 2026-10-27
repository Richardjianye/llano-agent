"""自有 skill hub 的客户端接口 —— 阶段 11 · S11-C

客户端在这里发布技能、搜索技能、取技能包。对应 `tools/llano_skill_hub.py` 里
`LlanoHubSource` 的 search / inspect / fetch 三个方法，外加一个发布口。

## 为什么放在 /v1 而不是 /client

/client 下是不要求登录的辅助接口。这里每一条都必须知道是谁在调：发布要记归属
（部门技能指标就是从这些行里算出来的），取包要记热度。走 require_caller，
与转发接口同一把 API Key。

## 拒绝走真的 4xx，不走「HTTP 200 + code」

跟 skill_reports 同一个理由：调用方是**机器**。全站业务异常默认是 200 带
{code, message} 信封（给后台页面读的），但客户端的 `LlanoHubSource` 按
`2xx == 成功` 分支。拒绝要是回 200，一次「slug 不合法」会被当成「发布成功」，
而员工那边看到的是一切正常、hub 上什么都没有。

信封格式不变（AppError 照旧带 code/message），改的只是 HTTP 状态码。

## 搜索与取包为什么不做部门隔离

hub 的目的是把员工的工作经验变成**公司**资产，按部门切开就没有这个效果了。
归属信息（owner_dept_code）照样每条都带着，用于统计而不是用于过滤。
"""

import json

from db.inserts.skill_hub import publish_skill
from db.selects.skill_hub import get_skill_bundle, get_skill_meta, search_skills
from fastapi import APIRouter, Depends, Query, Request, Response
from loguru import logger
from model.database import ROLE_DEVELOPER, ROLE_PRESIDENT
from services import conversation_scope as scope_service
from services.skill_hub import SkillHubRejected, can_browse_all, normalize_slug, parse_publish
from utils.errors import AppError
from utils.gateway_auth import Caller, require_caller
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter()


class SkillUseEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    slug: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class SkillUseBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_uid: str = Field(min_length=1, max_length=36)
    events: list[SkillUseEvent] = Field(min_length=1, max_length=100)


class SkillMetricsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slugs: list[str] = Field(min_length=1, max_length=100)


@router.post("/skill-hub/metrics")
async def skill_metrics(body: SkillMetricsRequest, caller: Caller = Depends(require_caller)):
    from db.selects.skill_hub import metrics_for_slugs
    try:
        slugs = [normalize_slug(slug) for slug in body.slugs]
    except SkillHubRejected as exc:
        raise _rejected(str(exc))
    return {"metrics": await metrics_for_slugs(slugs, dept_id=caller.dept_id,
        all_departments=can_browse_all(caller.role, caller.dept_code))}


@router.get("/skill-hub/usage-identity")
async def usage_identity(caller: Caller = Depends(require_caller)):
    return {"uid": caller.uid, "scope": scope_service.scope_from_credential(caller)}


@router.post("/skill-hub/usage")
async def usage_events(body: SkillUseBatch, request: Request, caller: Caller = Depends(require_caller)):
    if body.expected_uid != caller.uid:
        raise _rejected("调用记录属于另一账号，不能跨账号同步", status=403)
    if not scope_service.resolve_scope(caller, request.headers.get(scope_service.SCOPE_HEADER)).allows_capabilities:
        raise _rejected(scope_service.SCOPE_DENIED_MESSAGE, status=403)
    allowed = can_browse_all(caller.role, caller.dept_code)
    visible = set()
    for slug in {event.slug for event in body.events}:
        if await get_skill_meta(slug, dept_id=caller.dept_id, all_departments=allowed):
            visible.add(slug)
    accepted_events = [event for event in body.events if event.slug in visible]
    rejected = [event.event_id for event in body.events if event.slug not in visible]
    from services.skill_hub_usage import record_events
    try:
        accepted = await record_events(caller.uid, caller.api_key_id, accepted_events)
    except AppError as exc:
        raise _rejected(exc.message)
    return {"accepted": accepted, "rejected": rejected}

# 能越过「只有发布者能发新版」的角色。与 release:edit 同一档：
# 发布一个全公司都会装的技能，和推一个全公司都会装的安装包，是同一量级的动作。
_ADMIN_ROLES = frozenset({ROLE_PRESIDENT, ROLE_DEVELOPER})


def _rejected(message: str, *, status: int = 400) -> AppError:
    """业务码沿用 10006（参数不合法），但 HTTP 状态是真的 4xx —— 见模块说明。"""
    return AppError(code=10006, message=message, http_status=status)


def _no_store(response: Response) -> None:
    # 技能内容会变（同一个 slug 明天是新版本），中间层缓存住等于把旧版钉死。
    response.headers["Cache-Control"] = "no-store"


@router.post("/skill-hub/skills", status_code=201)
async def publish(
    request: Request,
    response: Response,
    caller: Caller = Depends(require_caller),
):
    """发布一个技能或它的新版本。

    201 = 真的新增了一版；200 = 内容与已有某版完全相同，什么都没变。
    这个区分是给人看的（CLI 会把它变成两句不同的话）：员工按了发布却什么都没发生，
    必须说清楚是「已经是最新的」而不是「发出去了」。
    """
    _no_store(response)

    # CUSTOM（决策 #45 / v2 专章 P0-1）：群聊里不许发布。
    #
    # 发布是一个**以主人名义、对同部门可见**的写动作 —— 群里一句夹带注入的话
    # 就能让 agent 用主人的身份往公司技能库里塞东西，而技能是会被执行的。
    # 这正是 #45「能力不跨界，只有产物跨界」要挡的那一类。
    #
    # ⚠️ 只挡发布，**不挡搜索/取包**。那几条读的是调用者本来就看得见、装得上的
    #   东西（本部门及公司空间），挡了只是给协作场景添堵，换不来任何东西。真正的约束在客户端那一侧：
    #   群聊里的 agent 根本不该有 skill 能力（见 TODO 阶段 12 的「另一半」）。
    decision = scope_service.resolve_scope(
        caller, request.headers.get(scope_service.SCOPE_HEADER)
    )
    if not decision.allows_capabilities:
        raise _rejected(scope_service.SCOPE_DENIED_MESSAGE, status=403)

    body = await request.body()
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _rejected(f"载荷不是合法 JSON：{exc}")

    try:
        parsed = parse_publish(payload, raw_bytes=len(body), require_chinese_metadata=False)
    except SkillHubRejected as exc:
        raise _rejected(str(exc))

    from db.inserts.skill_hub import preflight_publish
    try:
        await preflight_publish(parsed.slug, caller.uid, caller.role in _ADMIN_ROLES)
    except SkillHubRejected as exc:
        raise _rejected(str(exc), status=403)
    try:
        from services.skill_hub_metadata import complete_publish_metadata
        parsed, metadata_repaired = await complete_publish_metadata(parsed, request, caller)
    except SkillHubRejected as exc:
        raise _rejected(str(exc))

    try:
        result = await publish_skill(
            parsed=parsed,
            publisher_uid=caller.uid,
            publisher_username=caller.username,
            publisher_dept_id=caller.dept_id,
            publisher_dept_code=caller.dept_code,
            is_admin=caller.role in _ADMIN_ROLES,
        )
    except SkillHubRejected as exc:
        # 「不是拥有者」「已下架」都是 403 而不是 400：载荷本身没问题，
        # 是这个人此刻没资格做这件事。客户端据此提示的话术完全不同。
        raise _rejected(str(exc), status=403)

    if not result.get("created"):
        response.status_code = 200

    result.setdefault("name", parsed.name)
    result.setdefault("description", parsed.description)
    result["metadata_repaired"] = metadata_repaired

    logger.info(
        f"skill hub 发布：{result['slug']} v{result['version']}"
        f"（{'新增' if result.get('created') else '内容未变'}，"
        f"发布人 {caller.username or caller.uid}）"
    )
    return result


@router.get("/skill-hub/skills")
async def list_skills(
    response: Response,
    q: str = Query("", description="关键词，匹配 slug / 名称 / 说明"),
    limit: int = Query(20, ge=1, le=100),
    caller: Caller = Depends(require_caller),
):
    """搜索已上架的技能。不含文件内容 —— 那是 bundle 接口的事。"""
    _no_store(response)
    # 只返回这个人看得到的：公司空间 ∪ 自己部门的空间（阶段 21）。
    # 部门从凭据解出来，不听客户端 —— 否则报一个别的部门就能看别人的技能。
    return {"skills": await search_skills(q, limit, dept_id=caller.dept_id,
        all_departments=can_browse_all(caller.role, caller.dept_code))}


@router.get("/skill-hub/catalog")
async def catalog(
    response: Response,
    department_id: int | None = Query(None, ge=0),
    offset: int = Query(0, ge=0),
    limit: int = Query(6, ge=1, le=100),
    caller: Caller = Depends(require_caller),
):
    from db.selects.skill_hub import department_catalog
    _no_store(response)
    return await department_catalog(viewer_dept_id=caller.dept_id,
        department_id=department_id, offset=offset, limit=limit,
        all_departments=can_browse_all(caller.role, caller.dept_code))


@router.get("/skill-hub/preflight/{slug}")
async def publish_preflight(slug: str, request: Request, response: Response,
                            caller: Caller = Depends(require_caller)):
    """发布前只读预检：available / yours / taken / delisted。不写库、不调 AI。

    与发布同一道群聊闸门：协作会话里本来就不许发布，预检也不回答。
    """
    _no_store(response)
    decision = scope_service.resolve_scope(caller, request.headers.get(scope_service.SCOPE_HEADER))
    if not decision.allows_capabilities:
        raise _rejected(scope_service.SCOPE_DENIED_MESSAGE, status=403)
    try:
        normalized = normalize_slug(slug)
    except SkillHubRejected as exc:
        raise _rejected(str(exc))
    from db.inserts.skill_hub import publish_status
    return await publish_status(normalized, caller.uid, caller.role in _ADMIN_ROLES)


@router.get("/skill-hub/mine")
async def my_skills(response: Response, caller: Caller = Depends(require_caller)):
    """调用者首发的技能（含已下架），给作者的「我发布的技能」管理页。只读。

    路径放在 `/skill-hub/skills/{slug}` 之外：mine 不是一个合法的技能 slug，
    但放进同一前缀会和 slug 路由抢匹配。
    """
    from db.selects.skill_hub import list_owned_skills
    _no_store(response)
    return {"skills": await list_owned_skills(caller.uid)}


@router.get("/skill-hub/skills/{slug}")
async def inspect_skill(
    slug: str,
    response: Response,
    caller: Caller = Depends(require_caller),
):
    """单个技能的元数据。对应 SkillSource.inspect —— 不下载文件。"""
    _no_store(response)
    try:
        normalized = normalize_slug(slug)
    except SkillHubRejected as exc:
        raise _rejected(str(exc))

    meta = await get_skill_meta(normalized, dept_id=caller.dept_id,
        all_departments=can_browse_all(caller.role, caller.dept_code))
    if meta is None:
        # 已下架和从不存在都回 404，不区分。区分开等于告诉调用方
        # 「有这么个技能，只是你看不到」，而下架的用途就是让它不存在。
        raise _rejected(f"hub 上没有 {normalized}", status=404)
    return meta


@router.get("/skill-hub/skills/{slug}/bundle")
async def fetch_bundle(
    slug: str,
    response: Response,
    purpose: str = Query("install", pattern="^(install|update)$"),
    caller: Caller = Depends(require_caller),
):
    """取技能包本体。对应 SkillSource.fetch。

    `purpose=update`：把已装的技能换成新版本，不计下载量（见 get_skill_bundle）。
    """
    _no_store(response)
    try:
        normalized = normalize_slug(slug)
    except SkillHubRejected as exc:
        raise _rejected(str(exc))

    bundle = await get_skill_bundle(normalized, dept_id=caller.dept_id,
        all_departments=can_browse_all(caller.role, caller.dept_code),
        count_download=purpose != "update")
    if bundle is None:
        raise _rejected(f"hub 上没有 {normalized}", status=404)

    # files_json 在库里是一整串，这里解开成数组再交给 FastAPI 序列化。
    #
    # 直接把字符串塞进 "files_json" 字段传下去能省一次解析+序列化，但那样
    # wire protocol 里就有一个「值是 JSON 的字符串」字段，客户端得二次 json.loads。
    # 取包是低频动作（员工偶尔装一个技能），不值得为这点 CPU 换一个别扭的协议。
    raw = bundle.pop("files_json", "[]")
    try:
        bundle["files"] = json.loads(raw)
    except ValueError:
        # 库里那一行坏了。对客户端来说等同于「没有」——给它一个解不开的包，
        # 它会在安装到一半的时候炸，比这里直接说没有更难查。
        logger.error(f"skill hub：{normalized} 的 files_json 解不开，这一行数据坏了")
        raise _rejected(f"{normalized} 的技能包数据损坏，请联系管理员", status=500)

    return bundle
