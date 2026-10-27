from typing import Optional

from pydantic import BaseModel


class UserLogin(BaseModel):
    username: str
    password: str


class RefreshToken(BaseModel):
    # 留空时后端回退读 httpOnly Cookie（前端刷新页面后内存令牌已丢失）
    refresh_token: Optional[str] = None


class LogoutTokens(BaseModel):
    # 桌面客户端不使用 Cookie，必须能显式提交 refresh token 才能完整撤销。
    refresh_token: Optional[str] = None


class UserInfo(BaseModel):
    uid: Optional[str] = None
    username: Optional[str] = None
    nickname: Optional[str] = None
    realname: Optional[str] = None
    status: Optional[int] = 0
    oldPassword: Optional[str] = None
    newPassword: Optional[str] = None
