import os
from functools import lru_cache
from typing import Optional

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置 - 通过 .env / .env.dev / .env.prod 加载"""

    model_config = SettingsConfigDict(
        env_file=[".env"],
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "dev"
    log_level: Optional[str] = "INFO"
    base_url: str = "localhost"
    app_name: str = "audit-gateway"

    # ---- 监听地址（改端口记得同步前端 vite.config.mts 的 GATEWAY_ORIGIN）----
    host: str = "localhost"
    port: int = 7899
    # Whole encoded request, including base64 and all attachments. Independent
    # from the per-file audit retention limit below.
    request_body_max_bytes: int = Field(default=32 * 1024 * 1024, gt=0)

    # ---- MySQL 数据库 ----
    mysql_url: str
    db_echo: Optional[bool] = False
    db_pool_size: Optional[int] = 20
    db_max_overflow: Optional[int] = 30
    db_pool_timeout: Optional[int] = 60
    pool_recycle: Optional[int] = 900

    # ---- Redis ----
    redis_url: str = "redis://localhost:6379/0"
    redis_sock: Optional[str] = None  # 若有，优先用 sock

    # ---- JWT 身份验证 ----
    jwt_secret_key: str = "secret"
    jwt_refresh_secret_key: str = "secret"
    access_token_expire_minutes: Optional[int] = 60
    refresh_token_expire_minutes: Optional[int] = 10080  # 7 天
    # 设备 API Key 闲置失效天数：超过这么多天没有任何 /v1 调用的个人 Key 自动失效。
    # 0 = 关闭。实际生效值不会低于 refresh token 有效期 + 1 天 —— 见
    # services/apikey_lifecycle.effective_idle_days，那是「不踢掉在用客户端」的保证。
    api_key_idle_days: int = Field(default=30, ge=0, le=3650)
    # 闲置 Key 自动吊销的巡检间隔（秒），0 = 不巡检（认证时碰到仍会当场吊销）
    api_key_idle_sweep_interval_seconds: int = Field(default=3600, ge=0)
    # 对外开放接口（/open/v1）每个接入应用每分钟的调用上限
    open_api_rate_limit_per_minute: int = Field(default=120, ge=1)
    algorithm: Optional[str] = "HS256"

    # ---- 企业身份登录（钉钉优先；其他平台共用 external_identities 映射）----
    # Client Secret 只放部署环境；回调地址必须与钉钉后台配置完全一致。
    dingtalk_client_id: Optional[str] = None
    dingtalk_client_secret: Optional[str] = None
    dingtalk_corp_id: Optional[str] = None
    dingtalk_redirect_uri: Optional[str] = None

    # ---- 企业统一登录：独立于管理后台 JWT 的 OIDC 发admin ----
    # 私钥只放服务端密钥存储；可用 PEM 内容（换行写成 \\n）或绝对文件路径。
    sso_issuer: Optional[str] = None
    sso_org_id: Optional[str] = None
    sso_signing_private_key: Optional[str] = None
    sso_signing_key_id: Optional[str] = None
    sso_public_key_archive_dir: Optional[str] = None  # 轮换期仍发布旧公钥，直到旧 ID Token 全部过期

    # 静态产物文件必须存私有持久卷；不能放在 /static 或管理后台 dist 下。
    artifact_storage_path: Optional[str] = None
    # ---- 可逆加密密钥（见 utils/secret_box.py）----
    # 中台等外部系统凭证，密钥必须与 JWT / 上游 Key 分开保管（TODO 决策 #5）
    credential_encryption_key: Optional[str] = None
    credential_encryption_keys_old: Optional[str] = None  # 轮换期旧密钥，逗号分隔
    # 上游模型服务 API Key
    provider_encryption_key: Optional[str] = None
    provider_encryption_keys_old: Optional[str] = None

    # Codex 渠道掉号巡检间隔（秒）。0 = 关闭巡检（只靠流量侧被动检测）
    codex_health_interval_seconds: int = 600

    # ---- 上游模型渠道 ----
    # 供 scripts/bootstrap.py 首次登记渠道使用；登记后 Key 已加密入库，
    # 之后改 Key 走后台「模型与白名单 → 上游渠道」，不必再动这里
    newapi_base_url: Optional[str] = None
    newapi_api_key: Optional[str] = None

    # ---- 项目仓库（自建 Gitea）----
    # Token 只放部署环境，不入库、不下发客户端。未配置时项目仍可创建，仓库状态会
    # 明确落为 failed，管理员补好配置后可重试。
    gitea_base_url: Optional[str] = None
    gitea_api_token: Optional[str] = None
    gitea_repo_owner: str = "llano-agent"
    gitea_timeout_seconds: float = Field(default=15.0, gt=0, le=60)

    # ---- 发送方向脱敏（决策 #9）----
    # 逗号分隔的关键词，出网前替换成 mask_placeholder。
    # 客户端本地会先做一遍，这里是服务端兜底，防客户端被绕过
    mask_keywords: str = ""
    mask_placeholder: str = "[公司]"

    # ---- 会话附件留存（决策 #8「含上传文件/图片记录」）----
    # 多模态请求里的图片/文件抽出来单独落盘，会话原文里只留占位符。
    # 关掉 store 只影响**文件本体**：元数据（文件名、大小、哈希）照样入库，
    # 审计员仍能看到「他发过一个 3MB 的 xlsx」。
    audit_storage_path: Optional[str] = os.path.join(
        os.path.dirname(__file__), "static", "audit"
    )
    audit_store_attachment_bytes: bool = True
    # 单文件上限，超出只记元数据。
    #
    # 2026-09-07 从 20MB 降到 10MB，理由是内存不是磁盘：一个附件在处理期间
    # 会同时以三种形态存在（原始 body 字节、解析出的 base64 字符串、
    # b64decode 出来的原始字节 + 审计副本），**峰值约是文件本身的 3.5~4 倍**。
    # 按 20MB 算，单请求峰值约 75MB；生产机当时 MemAvailable 只有 738MB，
    # 十来个人同时传大文件就能把整机吃穿 —— 而所有服务都没设 cgroup 限制，
    # 被 OOM killer 挑中的未必是网关，可能是同机另一个项目的 MySQL。
    #
    # 10MB 对「截图 + 常见文档」够用（手机截图 base64 后一般 2~5MB），
    # 超出的仍然留元数据，审计员看得到「他发过一个 30MB 的文件」。
    audit_attachment_max_bytes: int = 10 * 1024 * 1024

    # ---- 桌面端自有更新（阶段 5）----
    # 更新清单的签名公钥（base64 的 32 字节 Ed25519 公钥）。
    #
    # 网关上**只有公钥，没有私钥**。私钥留在离线签名机上，发布时先在那里签好，
    # 把「清单 + 签名」一起提交给发布接口，网关用这把公钥验一遍才准入库。
    # 网关因此验得了、签不出来 —— 它被拿下也造不出客户端会接受的更新，
    # 而这正是签名要保证的性质。
    #
    # 这把公钥必须与客户端内置的那把是同一把（比对指纹，见
    # services/release_manifest.public_key_fingerprint）。不配就发不了版：
    # 发布接口会拒绝，而不是「跳过校验直接入库」。
    update_public_key: Optional[str] = None

    # ---- CORS ----
    # 生产环境允许的来源，逗号分隔；桌面客户端一般是 tauri:// 或 app:// 协议
    cors_allow_origins: str = ""

    # ---- 静态文件存储路径 ----
    avatar_path: Optional[str] = os.path.join(
        os.path.dirname(__file__), "static", "avatar"
    )

    # ---- 管理后台前端 ----
    # 前端构建产物目录（client/ 下 npm run build 的输出）。留空时按 ../client/dist 找。
    # 找不到就不托管，开发期用 vite dev server 即可 —— 那边自带到网关的代理。
    # 配了这一项就不需要 nginx：网关自己发前端，还省掉一次跨域。
    frontend_dist_path: Optional[str] = None

    @property
    def is_prod(self) -> bool:
        return self.environment == "prod"

    @property
    def cors_origin_list(self) -> list:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

@lru_cache
def get_settings():
    load_dotenv()
    env_file = ".env.dev"
    if os.getenv("ENVIRONMENT") == "prod":
        env_file = ".env.prod"
    return Settings(_env_file=[".env", env_file])
