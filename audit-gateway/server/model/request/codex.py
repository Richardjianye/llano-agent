"""Codex 渠道管理请求体"""

from typing import Optional

from pydantic import BaseModel, Field


class CodexOAuthStart(BaseModel):
    """发起 OAuth 登录（第一步）"""

    redirect_uri: Optional[str] = Field(
        None,
        description="回调地址；默认 http://localhost:1455/auth/callback（Codex CLI 官方口径）",
    )


class CodexOAuthExchange(BaseModel):
    """授权码兑换并落库（第二步）"""

    session_id: str = Field(..., min_length=1, description="oauth/start 返回的会话号")
    code: str = Field(
        ..., min_length=1,
        description="授权后粘贴含 code 和 state 的完整回调 URL",
    )
    provider_code: str = Field(..., min_length=1, max_length=64, description="渠道编码，如 codex-main")
    name: Optional[str] = Field(None, max_length=64, description="渠道展示名；留空用账号邮箱")
    base_url: Optional[str] = Field(
        None, description="高级：自建中转地址（codex://host[/path]）；留空用官方后端"
    )


class CodexProviderImport(BaseModel):
    """直接导入 refresh_token（跳过浏览器授权）"""

    provider_code: str = Field(..., min_length=1, max_length=64)
    refresh_token: str = Field(..., min_length=1, description="Codex/ChatGPT 账号的 refresh_token")
    name: Optional[str] = Field(None, max_length=64)
    base_url: Optional[str] = Field(None, description="高级：自建中转地址；留空用官方后端")
