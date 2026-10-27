"""技能安全事件与样本的接收口 —— 阶段 7 · S7-C（上行）

品牌客户端在隔离、恢复或发布前扫描命中时把事件传到这里；
发布扫描提醒带实际发布包样本，供管理员事后核查。

## 本期只做上行

复核结论怎么回到客户端是下一期的事。所以这个接口不返回任何客户端会据以行动的
内容 —— 收下了就是 202，仅此而已。刻意不留一个「将来再说」的半成品字段：
客户端读到一个还没人负责写的字段，比读不到要糟。

## 为什么放在 /v1 而不是 /client

/client 下是不要求登录的辅助接口。这里必须知道是谁传的（这张表的全部用途就是
「谁越过了扫描结论」），所以走 require_caller，与转发接口同一把 API Key。

## 拒绝走真的 4xx，不走「HTTP 200 + code」

全站业务异常默认是 200 带一个 {code, message} 信封（给后台页面读的）。这个接口
刻意不这样：它的调用方是**机器**，客户端按 `2xx == 已送达` 决定把本地队列里那条
事件删掉（tools/skill_report.py 的 flush）。拒绝要是回 200，客户端就会把一条
**根本没入库**的留痕当成已经收妥然后删掉 —— 一个留痕系统最不能有的失败形态。

信封格式不变（AppError 照旧带 code/message），改的只是 HTTP 状态码。
"""

import json

from db.inserts.skill_report import insert_skill_event
from fastapi import APIRouter, Depends, Request, Response
from loguru import logger
from services.skill_report import SkillReportRejected, parse_event
from utils.errors import AppError
from utils.gateway_auth import Caller, require_caller

router = APIRouter()


def _rejected(message: str) -> AppError:
    """业务码沿用 10006（参数不合法），但 HTTP 状态是真的 400 —— 见模块说明。"""
    return AppError(code=10006, message=message, http_status=400)


@router.post("/skill-reports", status_code=202)
async def receive_skill_report(
    request: Request,
    response: Response,
    caller: Caller = Depends(require_caller),
):
    response.headers["Cache-Control"] = "no-store"

    body = await request.body()
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _rejected(f"载荷不是合法 JSON：{exc}")

    try:
        parsed = parse_event(payload, raw_bytes=len(body))
    except SkillReportRejected as exc:
        raise _rejected(str(exc))

    result = await insert_skill_event(
        parsed=parsed,
        user_uid=caller.uid,
        dept_id=caller.dept_id,
        dept_code=caller.dept_code,
    )

    if result["stored"]:
        # 这一行是要被人看的：技能被隔离意味着扫描器在员工机器上命中了东西。
        logger.warning(
            f"技能安全事件 {parsed.event}：{parsed.skill_name} "
            f"（{parsed.verdict or '无结论'}，上报人 {caller.uid}，设备 {parsed.device or '未知'}）"
        )

    return result
