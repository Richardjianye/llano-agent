"""Codex 渠道凭证存取与自动续期

凭证（refresh_token / access_token / 账号身份）打包成一个 JSON，
存进 upstream_providers.api_key_encrypted —— 与普通上游 API Key 同一条
Fernet 加密通道（SecretPurpose.PROVIDER），只进不出，日志不落明文。

续期纪律：
- access_token 过期前 60 秒提前续期；
- OpenAI 轮换 refresh_token，续期成功必须把新凭证写回库，否则下一轮用旧
  refresh_token 会直接 invalid_grant；
- 同一渠道并发续期用锁串成单飞（single-flight），两个请求同时拿旧
  refresh_token 去刷会互相作废。通过数据库行锁串行刷新，并在锁内重新读取最新凭证，支持多 worker。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from services.codex_channel import oauth
from services.codex_channel.oauth import AccountIdentity, OAuthLoginRequired, TokenSet
from utils.secret_box import SecretDecryptError, SecretPurpose, decrypt_secret, encrypt_secret

CREDENTIAL_KIND = "codex_oauth"
EXPIRY_SKEW_SECONDS = 60


class CredentialError(RuntimeError):
    """凭证形态不对（比如把普通 API Key 填进了 Codex 渠道）"""


@dataclass
class CodexCredential:
    """一条 Codex 渠道的完整凭证"""

    tokens: TokenSet
    identity: AccountIdentity = field(default_factory=AccountIdentity)

    def to_json(self) -> str:
        payload: Dict[str, Any] = {"kind": CREDENTIAL_KIND}
        payload.update(self.tokens.to_dict())
        payload.update(self.identity.to_dict())
        return json.dumps(payload, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "CodexCredential":
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise CredentialError(
                "Codex 渠道凭证不是合法 JSON：请用「导入 refresh_token」或 OAuth 登录重建凭证"
            ) from exc
        if not isinstance(data, dict) or not data.get("refresh_token"):
            raise CredentialError(
                "Codex 渠道凭证缺少 refresh_token：请用「导入 refresh_token」或 OAuth 登录重建凭证"
            )
        return cls(
            tokens=TokenSet.from_dict(data),
            identity=AccountIdentity(
                email=str(data.get("email") or ""),
                user_id=str(data.get("user_id") or ""),
                chatgpt_account_id=str(data.get("chatgpt_account_id") or ""),
                plan_type=str(data.get("plan_type") or ""),
            ),
        )

    def access_fresh(self, skew: int = EXPIRY_SKEW_SECONDS) -> bool:
        if not self.tokens.access_token:
            return False
        if not self.tokens.expires_at:
            # 没有到期时间就当不过期（外部导入的 AT 自己负责有效性）
            return True
        return time.time() < self.tokens.expires_at - skew


def load_credential(api_key_encrypted: Optional[str]) -> CodexCredential:
    """从渠道行解出凭证（只在内存里还原，不写日志）"""
    if not api_key_encrypted:
        raise CredentialError("Codex 渠道未配置凭证，请先登录或导入 refresh_token")
    try:
        raw = decrypt_secret(api_key_encrypted, SecretPurpose.PROVIDER)
    except SecretDecryptError as exc:
        raise CredentialError(f"凭证解密失败：{exc}") from exc
    return CodexCredential.from_json(raw)


def encrypt_credential(cred: CodexCredential) -> str:
    return encrypt_secret(cred.to_json(), SecretPurpose.PROVIDER)


# ==================== 续期（数据库行锁覆盖多 worker） ====================

async def save_credential(provider_id: int, cred: CodexCredential) -> None:
    from db.updates.catalog import update_provider
    await update_provider(provider_id, api_key=cred.to_json())


async def ensure_access_token(provider_id: int, api_key_encrypted: Optional[str]) -> Tuple[str, CodexCredential]:
    return await ensure_fresh(provider_id, load_credential(api_key_encrypted))


async def ensure_fresh(provider_id: int, cred: CodexCredential) -> Tuple[str, CodexCredential]:
    # 每个请求的渠道记录来自数据库；不复用进程缓存，重新授权后立即使用新凭据。
    if cred.access_fresh():
        return cred.tokens.access_token, cred
    from db import init_async_session
    from model.database import UpstreamProvider
    from sqlalchemy import select
    factory = await init_async_session()
    async with factory() as session:
        provider = (await session.execute(select(UpstreamProvider).where(
            UpstreamProvider.id == provider_id).with_for_update())).scalar_one_or_none()
        if provider is None or provider.status != 1 or not provider.base_url.lower().startswith("codex://"):
            raise CredentialError("Codex 渠道不存在或已停用")
        current = load_credential(provider.api_key_encrypted)
        if current.access_fresh():
            return current.tokens.access_token, current
        tokens, identity = await oauth.refresh_tokens(current.tokens.refresh_token)
        if identity.email or identity.chatgpt_account_id:
            current.identity = identity
        current.tokens = tokens
        # 与行锁在同一事务内写回，其他 worker 取得锁后重新读取轮换后的凭据。
        provider.api_key_encrypted = encrypt_credential(current)
        await session.commit()
        return current.tokens.access_token, current
