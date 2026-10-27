"""工作台浏览器登录交接：短期一次性码 + PKCE，令牌不经 URL。"""
import base64
import hashlib
import json
import secrets
from urllib.parse import urlparse, urlencode

from config import get_settings
from db.selects.user import get_user_info
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field
from router.admin.dingtalk import _start
from router.admin.user import mint_user_tokens
from utils.cache import CacheManager

router = APIRouter(prefix="/auth/native", tags=["工作台登录"])


@router.get("/authorize")
async def authorize(redirect_uri: str, state: str = Query(min_length=24, max_length=128),
                    code_challenge: str = Query(pattern=r"^[A-Za-z0-9_-]{43}$"),
                    code_challenge_method: str = "S256", provider: str = "dingtalk"):
    try:
        uri = urlparse(redirect_uri)
        valid = (uri.scheme == "http" and uri.hostname == "127.0.0.1" and uri.port
                 and uri.path == "/callback" and not uri.query and not uri.fragment
                 and not uri.username and not uri.password)
    except ValueError:
        valid = False
    if not valid or code_challenge_method != "S256" or provider != "dingtalk":
        raise HTTPException(400, "无效的工作台登录参数")
    mode = "native:" + json.dumps({"redirect_uri": redirect_uri, "state": state,
                                    "challenge": code_challenge})
    return await _start(mode)


async def handoff(mode: str, user_uid: str) -> str:
    pending = json.loads(mode[7:])
    code = secrets.token_urlsafe(32)
    payload = json.dumps({**pending, "uid": user_uid, "tenant_id": get_settings().dingtalk_corp_id})
    async with CacheManager.get_instance().redis_connection() as redis:
        stored = await redis.set("dingtalk:native:" + code, payload, ex=60, nx=True)
    if not stored:
        raise HTTPException(503, "无法完成工作台登录")
    return pending["redirect_uri"] + "?" + urlencode({"code": code, "state": pending["state"]})


class TokenExchange(BaseModel):
    code: str = Field(min_length=40, max_length=128)
    code_verifier: str = Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9._~-]+$")


@router.post("/token")
async def exchange(body: TokenExchange, response: Response):
    async with CacheManager.get_instance().redis_connection() as redis:
        raw = await redis.getdel("dingtalk:native:" + body.code)
    if not raw:
        raise HTTPException(401, "登录交接码已失效，请重新登录")
    pending = json.loads(raw)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(body.code_verifier.encode("ascii")).digest()).decode().rstrip("=")
    if not secrets.compare_digest(challenge, pending["challenge"]):
        raise HTTPException(401, "登录交接校验失败")
    if pending["tenant_id"] != get_settings().dingtalk_corp_id:
        raise HTTPException(401, "企业配置已变化，请重新登录")
    user = await get_user_info(uid=pending["uid"])
    if user is None or user.status != 1 or user.account_kind != "internal":
        raise HTTPException(403, "账号不可用，请联系管理员")
    access, refresh = await mint_user_tokens(user)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return {"access_token": access, "refresh_token": refresh, "provider": "dingtalk",
            "user_id": user.uid, "username": user.username}
