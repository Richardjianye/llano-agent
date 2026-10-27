"""SQLAlchemy ORM 模型

分组说明：
- 组织与身份：Department / User
- 模型路由：UpstreamProvider / ModelInfo / ModelWhitelist
- 外部凭证：PlatformCredential（中台账号，可逆加密存储，见 TODO 决策 #5）
"""

from datetime import datetime, timezone

import sqlalchemy
from sqlalchemy import (BigInteger, Boolean, Column, Date, DateTime, Index,
                        Integer, Numeric, String, Text, UniqueConstraint)
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME, MEDIUMTEXT
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase


def now() -> datetime:
    """全库统一的「当前时间」= **服务器本地时间**（naive）

    ⚠️ 这里曾经返回 UTC，是个真实存在过的 bug：

    - `conversation_logs.created_at` 等时间戳走这个函数 → 存 UTC
    - `usage_records.stat_date` 走 `date.today()` → 存服务器本地日

    两套口径混用，东八区下审计页显示的时间比实际早 8 小时；更麻烦的是
    每天 00:00~08:00 之间的调用，用量报表算作「今天」而审计记录写成「昨天」，
    两个页面对同一批请求给出不同的日期，对账时会得出完全错误的结论。

    统一成服务器本地时间而不是把 `date.today()` 改成 UTC，理由是：
    报表口径已经明确写成「服务器本地日」（见 services/usage_query.py），
    而看报表的是本地同事，按本地日切分才符合直觉。

    naive 存储不变：MySQL 的 DATETIME 不带时区，存 aware 值只会引入
    「这一列到底是哪个时区」的歧义。部署时保证服务器时区为 Asia/Shanghai 即可。
    """
    return datetime.now()


# 旧名保留一段时间：外部若有引用不至于直接 ImportError。
# 名字有误导性（它现在返回的不是 UTC），新代码一律用 now()。
utcnow = now


class Base(AsyncAttrs, DeclarativeBase):
    """SQLAlchemy 异步基类"""

    __table_args__ = {"mysql_engine": "InnoDB"}
    metadata: sqlalchemy.MetaData = sqlalchemy.MetaData()


class TimestampMixin:
    """创建/更新时间戳"""

    created_at = Column(DateTime, default=now, nullable=False, comment="创建时间")
    updated_at = Column(
        DateTime, default=now, onupdate=now, nullable=False, comment="更新时间"
    )


# ==================== 角色常量 ====================

ROLE_PRESIDENT = "president"  # 总裁：最高权限，不能被任何人管理
ROLE_DEVELOPER = "developer"  # 开发者：可视组件与权限和总裁一致，唯一区别是不能管理总裁账号
ROLE_AUDITOR = "auditor"      # 管理部门审计员：只读会话审计（决策 #8），按角色授权，不看部门
ROLE_MANAGER = "manager"      # 部门负责人
ROLE_EMPLOYEE = "employee"    # 普通员工

# 模板遗留值，仅为兼容历史数据，新账号不要再用
ROLE_ADMIN = "admin"          # 等同 developer
ROLE_OPERATOR = "operator"    # 等同 employee

ALL_ROLES = (
    ROLE_PRESIDENT,
    ROLE_DEVELOPER,
    ROLE_AUDITOR,
    ROLE_MANAGER,
    ROLE_EMPLOYEE,
)

LEGACY_ROLES = (ROLE_ADMIN, ROLE_OPERATOR)

ASSIGNABLE_ROLES = ALL_ROLES

# ---- 角色级别 ----
# 账号管理遵循一条规则：**只能操作级别严格低于自己的账号**，唯一例外是
# 开发者可以管理其他开发者（services/permissions.py 的 can_manage）。
# 由此推出：总裁可管理开发者；开发者可管理除总裁以外的所有账号，动不了总裁。
ROLE_LEVELS = {
    ROLE_PRESIDENT: 100,
    ROLE_DEVELOPER: 90,
    ROLE_ADMIN: 90,       # 遗留值与 developer 同级
    ROLE_AUDITOR: 60,
    ROLE_MANAGER: 40,
    ROLE_EMPLOYEE: 10,
    ROLE_OPERATOR: 10,    # 遗留值与 employee 同级
}


def role_level(role: str | None) -> int:
    """未知角色一律按最低级处理，避免脏数据提权"""
    if not role:
        return 0
    return ROLE_LEVELS.get(role, 0)

# 白名单作用域
SCOPE_GLOBAL = "global"
SCOPE_DEPT = "dept"
SCOPE_ROLE = "role"
SCOPE_USER = "user"

ALL_SCOPES = (SCOPE_GLOBAL, SCOPE_DEPT, SCOPE_ROLE, SCOPE_USER)


# ==================== 组织与身份 ====================


class Department(Base, TimestampMixin):
    """部门表 —— 模型路由与审计范围的基本单位"""

    __tablename__ = "departments"
    __table_args__ = {"mysql_collate": "utf8mb4_general_ci"}

    id = Column(Integer, primary_key=True, autoincrement=True, comment="自增ID")
    code = Column(String(64), unique=True, nullable=False, comment="部门编码，稳定标识")
    name = Column(String(64), nullable=False, comment="部门名称")
    parent_id = Column(Integer, nullable=True, comment="上级部门ID，顶级为 NULL")
    # 钉钉部门后续以"绑定"形式接入（决策 #4），先留字段避免二次迁移
    dingtalk_dept_id = Column(String(64), nullable=True, comment="钉钉部门ID，未绑定为空")
    status = Column(Integer, default=1, nullable=False, comment="状态 1正常 0停用")
    remark = Column(String(255), nullable=True, comment="备注")


class User(Base, TimestampMixin):
    """用户表 —— 本系统账号，密码 bcrypt 哈希（不可逆）"""

    __tablename__ = "users"
    __table_args__ = {"mysql_collate": "utf8mb4_general_ci"}

    id = Column(Integer, primary_key=True, autoincrement=True, comment="自增ID")
    uid = Column(String(36), unique=True, nullable=True, comment="用户ID")
    username = Column(String(36), unique=True, nullable=False, comment="登录用户名")
    password = Column(String(128), nullable=False, comment="密码 bcrypt 哈希")
    nickname = Column(String(36), default="用户", comment="账户昵称")
    realname = Column(String(36), comment="真实姓名")
    # 保留字段名 permission 以兼容既有 JWT 载荷与前端，取值见 ALL_ROLES
    permission = Column(
        String(16),
        default=ROLE_EMPLOYEE,
        nullable=False,
        comment="角色 president/developer/auditor/manager/employee",
    )
    dept_id = Column(Integer, nullable=True, comment="所属部门ID")
    dingtalk_user_id = Column(String(64), nullable=True, comment="钉钉用户ID，未绑定为空")
    status = Column(Integer, default=0, nullable=True, comment="状态 1正常 0禁用")
    last_login_at = Column(DateTime, nullable=True, comment="最后登录时间")
    account_kind = Column(String(16), nullable=False, default="internal", server_default="internal")
    must_change_password = Column(Boolean, nullable=False, default=False, server_default="0")


class ExternalIdentity(Base, TimestampMixin):
    """企业身份与本系统稳定 uid 的显式绑定；不通过姓名或手机号自动认领。"""

    __tablename__ = "external_identities"
    __table_args__ = (
        UniqueConstraint("provider", "tenant_id", "subject_id", name="uq_external_identity_subject"),
        UniqueConstraint("provider", "tenant_id", "user_uid", name="uq_external_identity_user"),
        UniqueConstraint("provider", "tenant_id", "provider_user_id", name="uq_external_identity_member"),
        {"mysql_collate": "utf8mb4_bin"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String(32), nullable=False, comment="dingtalk / wecom / feishu")
    tenant_id = Column(String(128), nullable=False, comment="企业或租户稳定 ID")
    subject_id = Column(String(128), nullable=False, comment="平台提供的稳定用户标识")
    user_uid = Column(String(36), nullable=False, index=True, comment="本系统用户 uid")
    provider_user_id = Column(String(128), nullable=True, comment="平台企业内成员 ID")


class SSOClient(Base, TimestampMixin):
    """管理员审核登记的 OIDC 接入应用；不接受公开动态注册。"""

    __tablename__ = "sso_clients"
    __table_args__ = {"mysql_collate": "utf8mb4_bin"}

    client_id = Column(String(96), primary_key=True)
    name = Column(String(128), nullable=False)
    owner_uid = Column(String(36), nullable=False)
    environment = Column(String(16), nullable=False)
    audit_contact = Column(String(128), nullable=False)
    client_type = Column(String(16), nullable=False)
    redirect_uris = Column(Text, nullable=False)
    allowed_scopes = Column(String(128), nullable=False, default="openid profile org")
    secret_hash = Column(String(64), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)


class PublishedSite(Base, TimestampMixin):
    """网关托管的静态页面；访问规则属于产物，不复用模型白名单。"""

    __tablename__ = "published_sites"
    __table_args__ = {"mysql_collate": "utf8mb4_bin"}

    id = Column(String(36), primary_key=True)
    title = Column(String(128), nullable=False)
    owner_uid = Column(String(36), nullable=False, index=True)
    visibility = Column(String(16), nullable=False, default="private")
    allowed_users = Column(Text, nullable=False, default="[]")
    allowed_depts = Column(Text, nullable=False, default="[]")
    status = Column(String(16), nullable=False, default="draft")
    latest_version = Column(Integer, nullable=False, default=0)
    published_version = Column(Integer, nullable=True)


class PublishedSiteVersion(Base):
    """不可变的静态文件清单；文件本体放在私有目录。"""

    __tablename__ = "published_site_versions"
    __table_args__ = (
        UniqueConstraint("site_id", "version", name="uq_published_site_version"),
        {"mysql_collate": "utf8mb4_bin"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    site_id = Column(String(36), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    source_kind = Column(String(16), nullable=False)
    files = Column(Text, nullable=False)
    bytes_total = Column(BigInteger, nullable=False)
    created_by = Column(String(36), nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)


class DirectorySnapshot(Base):
    """管理员只读名册快照；失败快照也保留，避免连续完整性被错误推断。"""
    __tablename__ = "directory_snapshots"
    __table_args__ = {"mysql_collate": "utf8mb4_bin"}
    id = Column(String(36), primary_key=True)
    provider = Column(String(32), nullable=False)
    tenant_id = Column(String(128), nullable=False, index=True)
    operator_uid = Column(String(36), nullable=False)
    created_at = Column(DateTime().with_variant(MYSQL_DATETIME(fsp=6), "mysql"), default=now, nullable=False)
    completed_at = Column(DateTime().with_variant(MYSQL_DATETIME(fsp=6), "mysql"), nullable=True)
    complete = Column(Boolean, default=False, nullable=False)
    payload = Column(Text().with_variant(MEDIUMTEXT, "mysql"), nullable=False)


class DirectoryOperation(Base):
    """每行事务的持久审计与幂等结果，不含密码或令牌。"""
    __tablename__ = "directory_operations"
    __table_args__ = (UniqueConstraint("snapshot_id", "row_key", name="uq_directory_operation_row"), {"mysql_collate": "utf8mb4_bin"})
    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_id = Column(String(36), nullable=False, index=True)
    row_key = Column(String(160), nullable=False)
    action = Column(String(16), nullable=False)
    user_uid = Column(String(36), nullable=False)
    operator_uid = Column(String(36), nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)


# ==================== 模型路由 ====================


class UpstreamProvider(Base, TimestampMixin):
    """上游模型服务 —— DeepSeek 直连 / 合作厂商 NewAPI 网关（决策 #2）"""

    __tablename__ = "upstream_providers"
    __table_args__ = {"mysql_collate": "utf8mb4_general_ci"}

    id = Column(Integer, primary_key=True, autoincrement=True, comment="自增ID")
    code = Column(String(64), unique=True, nullable=False, comment="供应商编码，如 deepseek")
    name = Column(String(64), nullable=False, comment="展示名称")
    base_url = Column(String(255), nullable=False, comment="OpenAI 兼容的 base_url")
    # API Key 需原样发给上游，故可逆加密存储，与用户密码走不同密钥
    api_key_encrypted = Column(Text, nullable=True, comment="API Key（Fernet 加密）")
    timeout_seconds = Column(Integer, default=60, nullable=False, comment="请求超时（秒）")
    status = Column(Integer, default=1, nullable=False, comment="状态 1启用 0停用")
    remark = Column(String(255), nullable=True, comment="备注")


class ModelInfo(Base, TimestampMixin):
    """可路由的模型清单"""

    __tablename__ = "models"
    __table_args__ = (
        UniqueConstraint("provider_id", "model_name", name="uq_model_provider_name"),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True, comment="自增ID")
    provider_id = Column(Integer, nullable=False, comment="所属上游供应商ID")
    model_name = Column(String(128), nullable=False, comment="上游真实模型名")
    display_name = Column(String(128), nullable=False, comment="客户端展示名")
    # 客户端只能选网关下发的模型（决策 #7），别名让上游换型不影响客户端
    alias = Column(
        String(128), unique=True, nullable=True, comment="对外别名，为空则用 model_name"
    )
    supports_stream = Column(Boolean, default=True, nullable=False, comment="是否支持流式")
    # 原名 supports_vision。改名是因为它管的从来就不只是图片：
    # 豆包这类模型的视频、文档、音频走的是同一条 content parts 通道（都用 base64 内联），
    # 叫「支持图片」会让人以为传音频要另开一个开关
    supports_multimodal = Column(
        Boolean, default=False, nullable=False, comment="是否支持多模态输入（图片/音频/视频/文档）"
    )
    max_context_tokens = Column(Integer, nullable=True, comment="上下文长度")

    # ==================== 计价 ====================
    # NULL 表示「未配置」，与 0（免费）是两回事：报表要能告诉用户
    # 「这部分花费算不出来」，而不是悄悄按 0 计入总额（决策 #22）。
    # DECIMAL 不用 FLOAT：金额算术不能有二进制浮点误差。
    #
    # 计价模式（决策 #27，部分修订决策 #21）：
    #   token —— 按 token 计价，用下面三个 per_1m 单价
    #   per_call —— 按次计价，每次调用固定金额，与 token 数无关
    # 「分段/阶梯计价」仍然不做，需要时再说。
    billing_mode = Column(
        String(16), default="token", nullable=False, comment="计价模式 token/per_call"
    )
    input_price_per_1m = Column(
        Numeric(12, 6), nullable=True, comment="输入单价（元/百万 token），NULL 为未配置"
    )
    output_price_per_1m = Column(
        Numeric(12, 6), nullable=True, comment="输出单价（元/百万 token），NULL 为未配置"
    )
    # 缓存命中的输入 token 通常便宜一大截（各家 1/10 ~ 1/4 不等）。
    # **留空时按输入单价算**，不按 0 算 —— 留空的含义是「没单独配」，
    # 而不是「缓存不要钱」；按 0 算会让费用凭空偏低且没人察觉。
    cached_input_price_per_1m = Column(
        Numeric(12, 6), nullable=True, comment="缓存命中输入单价（元/百万 token），留空则按输入单价计"
    )
    price_per_call = Column(
        Numeric(12, 6), nullable=True, comment="按次单价（元/次），billing_mode=per_call 时生效"
    )
    currency = Column(String(8), default="CNY", nullable=False, comment="计价货币")
    status = Column(Integer, default=1, nullable=False, comment="状态 1启用 0停用")
    remark = Column(String(255), nullable=True, comment="备注")


class TokenQuota(Base, TimestampMixin):
    """按用户/部门 + 模型设置的每日 token 配额。

    配额必须绑定模型：某模型额度耗尽后只拒绝该模型，用户仍可手动切换到其他模型。
    用户规则与部门规则同时生效，任意一条耗尽都拒绝请求。
    部门规则覆盖整个下级树；父子部门各自配置时，两条规则同时生效而不是互相覆盖。
    """

    __tablename__ = "token_quotas"
    __table_args__ = (
        UniqueConstraint("scope", "scope_value", "model_id", name="uq_token_quota_scope_model"),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    scope = Column(String(16), nullable=False, comment="作用域 user/dept")
    scope_value = Column(String(64), nullable=False, comment="用户 uid 或部门 id")
    model_id = Column(Integer, nullable=False, index=True, comment="模型 ID")
    daily_token_limit = Column(BigInteger, nullable=False, comment="每日 token 上限")
    status = Column(Integer, default=1, nullable=False, comment="状态 1启用 0停用")
    remark = Column(String(255), nullable=True, comment="备注")


class ModelWhitelist(Base, TimestampMixin):
    """模型白名单 —— 按部门/角色/单用户授权（决策 #6）

    生效规则见 services/model_access.py：
    - global 作用域对所有人生效
    - dept / role / user 三类授权取并集
    - allowed=False 为显式拒绝，优先级高于任何允许
    """

    __tablename__ = "model_whitelist"
    __table_args__ = (
        UniqueConstraint("scope", "scope_value", "model_id", name="uq_whitelist_entry"),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True, comment="自增ID")
    scope = Column(String(16), nullable=False, comment="作用域 global/dept/role/user")
    scope_value = Column(
        String(64),
        nullable=False,
        default="",
        comment="部门ID/角色名/用户uid，global 为空串",
    )
    model_id = Column(Integer, nullable=False, comment="模型ID")
    allowed = Column(Boolean, default=True, nullable=False, comment="True 允许 False 显式拒绝")
    remark = Column(String(255), nullable=True, comment="备注")


# ==================== 外部凭证 ====================


class PlatformCredential(Base, TimestampMixin):
    """中台等外部系统凭证 —— 需要代登，必须可逆加密（决策 #5、#11）

    安全约束：
    - 使用独立密钥 CREDENTIAL_ENCRYPTION_KEY，与 JWT / 上游 Key 分离
    - 已认证用户只能绑定、测试、清除自己的凭据；管理员不能读取明文或代用户调用
    - 任何解密读取与凭据变更都要写 CredentialAccessLog
    - key_version 支持密钥轮换时分批重加密
    """

    __tablename__ = "platform_credentials"
    __table_args__ = (
        UniqueConstraint("user_uid", "platform", name="uq_credential_user_platform"),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True, comment="自增ID")
    user_uid = Column(String(36), nullable=False, comment="本系统用户uid")
    platform = Column(String(32), nullable=False, comment="外部平台标识，如 datahub")
    account = Column(String(128), nullable=False, comment="外部平台账号")
    secret_encrypted = Column(Text, nullable=False, comment="密码（Fernet 可逆加密）")
    key_version = Column(Integer, default=1, nullable=False, comment="加密密钥版本")
    status = Column(Integer, default=1, nullable=False, comment="状态 1有效 0失效")
    last_verified_at = Column(DateTime, nullable=True, comment="最后一次验证可用的时间")


# ==================== 客户端凭证 ====================


class ApiKey(Base, TimestampMixin):
    """网关签发的 API Key —— 供桌面客户端当 OpenAI 兼容端点用

    桌面端不方便带 Cookie，也不适合把用户密码存在本地，所以另发一把可吊销、
    可限速的 Key。存哈希不存明文：明文只在签发时回显一次。
    按 key_prefix 建索引做快速定位，再比对哈希，避免全表扫。
    """

    __tablename__ = "api_keys"
    __table_args__ = {"mysql_collate": "utf8mb4_general_ci"}

    id = Column(Integer, primary_key=True, autoincrement=True, comment="自增ID")
    user_uid = Column(String(36), nullable=False, index=True, comment="所属用户uid")
    name = Column(String(64), nullable=False, comment="用途备注，如「我的 Mac」")
    key_prefix = Column(
        String(24), unique=True, nullable=False, comment="明文前缀，用于定位与展示"
    )
    key_hash = Column(String(64), nullable=False, comment="完整明文的 sha256")
    status = Column(Integer, default=1, nullable=False, comment="状态 1有效 0已吊销")
    expires_at = Column(DateTime, nullable=True, comment="过期时间，空为永不过期")
    total_token_limit = Column(
        BigInteger, nullable=True, comment="该 Key 的累计 token 上限，空为不单独限制"
    )
    last_used_at = Column(DateTime, nullable=True, comment="最后使用时间")

    # ---- 吊销记录（2026-10-19）----
    # 2026-09 夜间设备凭据被成批作废，事后只能从日志推断是「客户端自己吊销」。
    # 记下谁、什么时候、为什么吊销，后台一眼能分清客户端退出、管理员操作、闲置清理。
    revoked_at = Column(DateTime, nullable=True, comment="吊销时间")
    revoke_reason = Column(
        String(32), nullable=True,
        comment="client_logout/admin/admin_batch/idle_<N>d/account_disabled/password_reset/self_password_change",
    )
    revoked_by = Column(String(36), nullable=True, comment="操作者uid；客户端自助吊销为空")

    # ---- 群聊会话令牌（阶段 24）----
    #
    # 阶段 12 把「作用域只能收紧」这条规则和它的执行点钉死了，但**凭据侧恒为
    # private** —— 那半刀等的就是这两列。
    #
    # 在此之前，「这一轮是群聊」只能由客户端发 X-Llano-Conversation-Scope 声明，
    # 而一个被控制（或只是忘了发头）的客户端不发就静默降级成 private，
    # 正好是那条防线要挡的路。
    #
    # 有了这两列，约束长在**凭据本身**上：拿群聊令牌发的请求，不管头里写什么，
    # 服务端都推得出 group。见 services/conversation_scope.scope_from_credential。
    scope = Column(
        String(16), default="private", nullable=False,
        comment="private=个人设备Key group=群聊会话令牌",
    )
    scope_project_id = Column(
        Integer, nullable=True, index=True,
        comment="群聊令牌绑定的项目ID；private 恒为空",
    )


# ==================== 会话审计与用量 ====================


class ConversationLog(Base):
    """会话内容审计 —— 仅管理部门可见（决策 #8）

    内容与指标**分表存放**：这张表装聊天原文，访问面极窄；
    usage_records 只有指标，部门负责人看报表时不会顺带碰到内容。
    """

    __tablename__ = "conversation_logs"
    __table_args__ = (
        # 会话折叠永远是「某个人的某段会话」，单列索引选择性不够 ——
        # 而且把 user_uid 放在前面，是在索引层面就复述那条硬规矩：
        # 串联限定在同一 user_uid 内
        Index("ix_conversation_logs_user_session", "user_uid", "session_id"),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="自增ID")
    request_id = Column(String(64), nullable=False, index=True, comment="请求ID，串联日志")
    user_uid = Column(String(36), nullable=False, index=True, comment="发起人uid")
    # 客户端给的 Hermes 会话 ID（决策 #52）。一次提问会散成十几条记录 ——
    # 标题生成 1 条、主对话 N 个 agent 轮次、命令审批扫描若干条 —— 靠它串成一段对话。
    #
    # ⚠️ **可伪造，绝不参与鉴权。** 只用于展示层的分组，且分组**必须限定在同一
    # user_uid 内**：否则填别人的 session_id 就能把自己的记录挂到别人的会话下。
    # 入库前按 `^[A-Za-z0-9_-]{1,64}$` 校验，不合规存 NULL
    # （见 services/audit_envelope.py）。
    #
    # NULL 是**正常状态而非异常**：stock Hermes、绕过我们客户端的直连调用都没有它。
    # 这类记录在列表页退化成「一行一条」照常显示，不能因为分不了组就消失
    session_id = Column(
        String(64), nullable=True, index=True, comment="Hermes 会话ID（客户端给，不可信，仅用于串联展示）"
    )
    # chat / title / approval / other。白名单外一律落 other，
    # 别让客户端字符串把这一列变成无界枚举。
    # **没带信封的调用默认 chat**（而不是 other）：other 那档在列表页默认隐藏，
    # 把来路不明的调用默认藏起来，方向正好反了
    turn_kind = Column(
        String(32), default="chat", nullable=False, comment="轮次类型：chat/title/approval/other"
    )
    dept_id = Column(Integer, nullable=True, comment="发起时所属部门（快照，不随调岗变）")
    role = Column(String(16), nullable=True, comment="发起时角色（快照）")
    model_id = Column(Integer, nullable=True, comment="模型ID")
    model_name = Column(String(128), nullable=True, comment="上游真实模型名")
    # ---- 内容：只留「问了什么 / 答了什么」（决策 #52）----
    #
    # 存的是脱敏后的版本：客户端替换 + 服务端兜底之后（决策 #9）。
    #
    # 这里曾经是一列 `request_messages`，装整个 messages 数组的 JSON。
    # 换掉它有两个理由：系统提示词每条记录一模一样、员工也改不了，全文入库纯占
    # 体积；而它排在数组第一个，把用户真正问的那句话挤到 20000 字额度之外去了。
    user_input = Column(Text, nullable=True, comment="本次用户输入全文（独占额度）")
    # 列表页摘要**必须是独立一列**，不能查询时现算：正文那几列是 TEXT，
    # 一页 20 条能拉回几 MB。保头也保尾（见 services/audit_content.py）
    last_user_preview = Column(
        String(512), nullable=True, comment="最后一条用户消息的摘要（列表页用，入库时落定）"
    )
    # 系统提示词只留指纹：一个 sha256 就够回答「这些记录用的是不是同一版提示词」，
    # 而这正是审计对它唯一会问的问题。NULL 表示这次调用根本没有系统提示 ——
    # 和「系统提示是空串」区分得开（空串的 sha256 是个看着很正常的 64 位十六进制）
    system_prompt_sha256 = Column(
        String(64), nullable=True, comment="系统提示词 sha256（只存指纹，不存全文）"
    )
    system_prompt_chars = Column(
        Integer, default=0, nullable=False, comment="系统提示词字数"
    )
    # 本地工具链只存骨架：`[{"name": …, "chars": …}]`。
    # ⚠️ 知情取舍（负责人 2026-08-21 拍板）：本地工具的结果确实出了网，
    # 停存正文后「什么公司数据被发给了模型」只剩体量不剩内容。保住的是
    # 「这次对话往模型里灌了 42KB 本地数据」这个信号。
    # MCP 调用不受影响，mcp_call_logs 单独审计
    tool_skeleton = Column(
        Text, nullable=True, comment='工具骨架 JSON [{"name","chars"}]，无工具时 NULL'
    )
    response_text = Column(Text, nullable=True, comment="回复正文（接收方向只存不改）")
    masked = Column(Boolean, default=False, nullable=False, comment="服务端是否兜底替换过")
    truncated = Column(Boolean, default=False, nullable=False, comment="内容是否被截断")
    status = Column(String(16), default="success", nullable=False, comment="success/failed")
    stream = Column(Boolean, default=False, nullable=False, comment="是否流式请求")
    # 「用户点了停止生成」不是失败：调用成功发生、token 照样消耗、内容也已产出。
    # 单独一列而不是塞进 status，是因为它和 success/failed 是**正交**的两件事 ——
    # 混进 status 会让「失败率」这个指标里混入一堆其实成功了的调用
    aborted = Column(
        Boolean, default=False, nullable=False, comment="客户端是否中途断开"
    )
    # 冗余一份附件数量：列表页要给带附件的行打标，为此每行去 join 一次附件表不值当
    attachment_count = Column(
        Integer, default=0, nullable=False, comment="本次请求携带的附件数（冗余，免 join）"
    )
    created_at = Column(DateTime, default=now, nullable=False, index=True, comment="发生时间")


class ConversationAttachment(Base):
    """会话附件记录 —— 决策 #8 的「含上传文件/图片记录」那一半

    **为什么必须单独一张表**：多模态消息里的图片常以 base64 data URL 内联，
    一张手机截图就有几 MB。原样塞进 `conversation_logs.request_messages` 会
    一次性撑爆那一列（且 CONTENT_MAX_CHARS 会把整条消息截断，连带把后面的
    文字也吃掉）。所以抽出来单独存，原文里只留一个 `[[attachment:...]]` 占位符。

    **落盘与元数据分开**：`sha256` + `size_bytes` + `filename` 永远记；
    文件本体是否真的落盘由 `stored` 标记，配置关掉（audit_store_attachment_bytes=false）
    或超过体积上限时只留元数据。审计员至少能看到「他发过一个 3MB 的 xlsx」，
    这比什么都没有强得多。

    去重按 sha256：同一张图被反复发送只占一份磁盘，但每次请求各记一行
    （审计要的是「谁在什么时候发的」，不是「这个文件存在过」）。
    """

    __tablename__ = "conversation_attachments"
    __table_args__ = {"mysql_collate": "utf8mb4_general_ci"}

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="自增ID")
    request_id = Column(String(64), nullable=False, index=True, comment="请求ID，串联同一次调用")
    conversation_id = Column(
        BigInteger, nullable=True, index=True, comment="所属 conversation_logs.id"
    )
    user_uid = Column(String(36), nullable=False, index=True, comment="上传者uid")
    dept_id = Column(Integer, nullable=True, comment="上传时所属部门（快照）")
    # 占位符里用的短码，详情页靠它把正文里的标记和附件行对上
    marker = Column(String(64), nullable=False, comment="原文占位符短码")
    kind = Column(String(16), default="file", nullable=False, comment="image/file/audio")
    mime_type = Column(String(128), nullable=True, comment="MIME 类型")
    filename = Column(String(255), nullable=True, comment="原始文件名，取不到时按类型生成")
    size_bytes = Column(BigInteger, default=0, nullable=False, comment="字节数")
    sha256 = Column(String(64), nullable=True, index=True, comment="内容哈希，用于去重与取证")
    # 远程 URL 形态的附件（客户端传 https 链接而非 base64）只记 URL 不抓取：
    # 网关主动去下载用户给的任意地址是个 SSRF 面，不值得为审计完整性开这个口子
    origin_url = Column(String(1024), nullable=True, comment="远程附件的原始URL（不抓取）")
    storage_path = Column(String(512), nullable=True, comment="落盘相对路径，未落盘为空")
    stored = Column(Boolean, default=False, nullable=False, comment="文件本体是否已落盘")
    store_error = Column(String(255), nullable=True, comment="落盘失败原因，便于排查")
    created_at = Column(DateTime, default=now, nullable=False, index=True, comment="发生时间")


class OpenApiCallLog(Base):
    """对外开放接口（/open/v1）的调用记录（2026-10-19）

    第三方平台拿应用凭据调用，没有「人」这个主体，所以不复用 audit_access_logs
    （那张表的 actor 是员工 uid）。关键字只存长度与摘要：搜索词本身可能是人名，
    审计要的是「谁、什么时候、查了多少」，不是再存一份通讯录。
    """

    __tablename__ = "open_api_call_logs"
    __table_args__ = {"mysql_collate": "utf8mb4_general_ci"}

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True,
                autoincrement=True, comment="自增ID")
    client_id = Column(String(96), nullable=False, index=True, comment="接入应用 client_id")
    endpoint = Column(String(64), nullable=False, comment="接口，如 members.search")
    status = Column(String(16), nullable=False, comment="ok/denied/rate_limited/invalid")
    query_digest = Column(String(64), nullable=True, comment="查询条件 sha256，便于比对重复查询")
    query_summary = Column(String(255), nullable=True, comment="查询条件摘要（不含关键字原文）")
    result_count = Column(Integer, default=0, nullable=False, comment="返回条数")
    client_ip = Column(String(64), nullable=True, comment="来源IP")
    created_at = Column(DateTime, default=now, nullable=False, index=True, comment="发生时间")


class AuditAccessLog(Base):
    """会话查阅日志 —— 「看的人也被看」（决策 #8 明写，且是合规动作之一）

    与 CredentialAccessLog 分开：那张表管的是中台凭证解密，
    这张表管的是聊天内容查阅，留存期限和审批流程都不一样。

    action 取值与颗粒度的对应关系（选了「列表 + 详情抽屉」方案才能记这么细）：

    | action | 含义 | target_conversation_id |
    | ------ | ---- | ---------------------- |
    | `list` | 翻了一页列表（只看到摘要） | 空 |
    | `detail` | 展开了某一条的全文 | 该条 id |
    | `attachment` | 下载/预览了某个附件 | 附件所属会话 id |
    | `export` | 导出了 CSV | 空 |

    列表接口不下发全文，正是为了让 `detail` 这一行能精确到「看了谁的哪一条」。
    """

    __tablename__ = "audit_access_logs"
    __table_args__ = {"mysql_collate": "utf8mb4_general_ci"}

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="自增ID")
    actor_uid = Column(String(36), nullable=False, index=True, comment="查阅人uid")
    actor_role = Column(String(16), nullable=True, comment="查阅时角色（快照）")
    actor_dept_id = Column(Integer, nullable=True, comment="查阅时部门（快照）")
    action = Column(String(16), nullable=False, index=True, comment="list/detail/attachment/export")
    target_conversation_id = Column(
        BigInteger, nullable=True, index=True, comment="被查阅的会话ID，列表/导出为空"
    )
    target_attachment_id = Column(BigInteger, nullable=True, comment="被查阅的附件ID")
    target_user_uid = Column(
        String(36), nullable=True, index=True, comment="被查阅内容的归属人uid"
    )
    # 查询条件原样存 JSON：日后要回答「他是不是在专门盯着某个人」，
    # 靠的就是这一列里反复出现的 user_uid
    query_json = Column(Text, nullable=True, comment="查询条件原文 JSON")
    result_count = Column(Integer, default=0, nullable=False, comment="本次返回的条数")
    request_id = Column(String(64), nullable=True, comment="请求ID")
    client_ip = Column(String(64), nullable=True, comment="来源IP")
    user_agent = Column(String(255), nullable=True, comment="User-Agent，区分后台/脚本")
    created_at = Column(DateTime, default=now, nullable=False, index=True, comment="发生时间")


class UsageRecord(Base):
    """用量明细 —— 每次调用一行

    维度做成字段快照而不是关联查：调岗、改名之后历史报表不该跟着变。
    """

    __tablename__ = "usage_records"
    __table_args__ = (
        Index(
            "idx_usage_records_user_kind_created",
            "user_uid",
            "call_kind",
            "created_at",
        ),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="自增ID")
    request_id = Column(String(64), nullable=False, index=True, comment="请求ID")
    api_key_id = Column(Integer, nullable=True, index=True, comment="调用使用的 API Key，JWT 调用为空")
    stat_date = Column(Date, nullable=False, index=True, comment="统计日期（服务器本地日）")
    user_uid = Column(String(36), nullable=False, index=True, comment="用户uid")
    dept_id = Column(Integer, nullable=True, index=True, comment="部门ID（快照）")
    role = Column(String(16), nullable=True, comment="角色（快照）")
    provider_id = Column(Integer, nullable=True, comment="上游渠道ID")
    model_id = Column(Integer, nullable=True, index=True, comment="模型ID")
    model_name = Column(String(128), nullable=True, comment="上游真实模型名")
    stream = Column(Boolean, default=False, nullable=False, comment="是否流式")

    # ---- 协作口径（决策 #48，阶段 13 补上）----
    #
    # 决策 #48 当时写的是「现在就要动手，不等 v2」，但一直没落。补的理由没变：
    # 群聊里 agent 的路由打分是**真实 token 消耗但没有任何可见输出**。按现行口径它
    # 要么漏记（违反既有记账纪律），要么混进「请求数」与「上游成功率」——而后者是
    # 用来判断合作厂商稳不稳的指标，被一堆没人看见的打分调用稀释就废了。
    #
    # 所以 gate 调用照常计费，但默认**不计入**对话请求数与成功率。不做的话，某天
    # 报表请求数翻三倍，查半天发现全是打分。
    #
    # 「字段是加法，数据补不回来」——与决策 #21 同理。默认值让所有历史行与既有
    # 写入路径自动落到 chat，不需要回填。
    call_kind = Column(
        String(16), default="chat", nullable=False, index=True,
        comment="chat=对话（计入请求数与成功率）/ gate=群聊路由打分（只计费）/ agent=agent 自主发言",
    )
    trigger_source = Column(
        String(16), default="private", nullable=False,
        comment="private=私聊 / project=项目群聊；分不清个人使用与协作消耗的话报表没法拆",
    )

    status = Column(String(16), default="success", nullable=False, comment="success/failed")
    error_code = Column(String(64), nullable=True, comment="失败原因，便于排查上游问题")
    # 与 status 正交：客户端中途断开的调用**仍然是成功的调用**（token 已消耗、
    # 内容已产出），只是没人看完。塞进 status 会污染「上游失败率」——
    # 那个指标是用来判断合作厂商稳不稳的，掺进用户主动叫停就失去意义了
    aborted = Column(
        Boolean, default=False, nullable=False, comment="客户端是否中途断开"
    )
    prompt_tokens = Column(Integer, default=0, nullable=False, comment="输入 token")
    completion_tokens = Column(Integer, default=0, nullable=False, comment="输出 token")
    total_tokens = Column(Integer, default=0, nullable=False, comment="合计 token")
    # ---- token 细分 ----
    # 计费口径远比「输入×单价 + 输出×单价」复杂：缓存命中、缓存写入、推理 token
    # 各有各的价，且各家字段名还不一样。成本计算功能以后再做，
    # 但**这些数字不存下来就永远追不回来了**，所以现在先全量落库。
    cached_prompt_tokens = Column(
        Integer, default=0, nullable=False, comment="输入中命中缓存的 token（读缓存，通常更便宜）"
    )
    cache_write_tokens = Column(
        Integer, default=0, nullable=False, comment="写入缓存的 token（建缓存，通常更贵）"
    )
    reasoning_tokens = Column(
        Integer, default=0, nullable=False, comment="推理模型的思考 token"
    )
    # 兜底：上游 usage 对象**原样**存一份。上面几列是当下能认出来的字段，
    # 认不出来的（分时缓存 TTL 分档、按次计费、各家私有扩展）都在这里，
    # 将来定价规则怎么变，都能拿这份原始数据重算，不必回头改写入侧。
    usage_raw = Column(Text, nullable=True, comment="上游返回的 usage 原文 JSON")
    # 上游没返回 usage 时按字符数估算，报表要能区分开，别让估算值污染口径
    tokens_estimated = Column(
        Boolean, default=False, nullable=False, comment="token 是否为估算值"
    )
    latency_ms = Column(Integer, default=0, nullable=False, comment="端到端耗时（毫秒）")

    # ---- 单价快照 ----
    # 记的是「这次调用**当时**按什么价算的」。不存的话，报表只能拿今天的单价
    # 去乘历史的 token：厂商一降价，上个月那份已经发出去的报表再打开金额就变了，
    # 而且与厂商账单永远对不上。
    #
    # 单价与费用都存：单价用于事后解释「这个数怎么来的」，
    # 费用用于直接累加（日聚合表要的是能相加的数，不是能相乘的料）。
    billing_mode_snapshot = Column(
        String(16), nullable=True, comment="调用时的计价模式 token/per_call，NULL 为无快照"
    )
    input_price_per_1m_snapshot = Column(
        Numeric(12, 6), nullable=True, comment="调用时的输入单价（元/百万 token）"
    )
    output_price_per_1m_snapshot = Column(
        Numeric(12, 6), nullable=True, comment="调用时的输出单价（元/百万 token）"
    )
    cached_input_price_per_1m_snapshot = Column(
        Numeric(12, 6), nullable=True, comment="调用时的缓存命中输入单价，留空表示当时就按输入单价计"
    )
    price_per_call_snapshot = Column(
        Numeric(12, 6), nullable=True, comment="调用时的按次单价（元/次）"
    )
    # NULL = 「当时没配单价，算不出」，与 0.000000（确实是 0 元）是两回事。
    # 这是决策 #22「单价未配置时绝不按 0 计入」在明细层的落点。
    cost = Column(
        Numeric(18, 6), nullable=True, comment="本次调用费用（按当时单价），NULL 为算不出"
    )
    cost_currency = Column(String(8), nullable=True, comment="费用货币")

    created_at = Column(DateTime, default=now, nullable=False, comment="发生时间")


class UsageDaily(Base):
    """用量日聚合 —— 报表页只查这张表，避免扫明细

    粒度：日期 × 用户 × 模型。部门和角色作为冗余字段带上，便于按部门下钻。
    """

    __tablename__ = "usage_daily"
    __table_args__ = (
        UniqueConstraint("stat_date", "user_uid", "model_id", name="uq_usage_daily"),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="自增ID")
    stat_date = Column(Date, nullable=False, index=True, comment="统计日期")
    user_uid = Column(String(36), nullable=False, index=True, comment="用户uid")
    dept_id = Column(Integer, nullable=True, index=True, comment="部门ID")
    role = Column(String(16), nullable=True, comment="角色")
    model_id = Column(Integer, nullable=True, comment="模型ID")
    model_name = Column(String(128), nullable=True, comment="模型名")
    requests = Column(Integer, default=0, nullable=False, comment="请求数")
    success_count = Column(Integer, default=0, nullable=False, comment="成功数")
    failed_count = Column(Integer, default=0, nullable=False, comment="失败数")
    prompt_tokens = Column(BigInteger, default=0, nullable=False, comment="输入 token 合计")
    completion_tokens = Column(BigInteger, default=0, nullable=False, comment="输出 token 合计")
    total_tokens = Column(BigInteger, default=0, nullable=False, comment="合计 token")
    # 缓存命中的输入 token 也要聚合，否则报表层无法按缓存单价计价 ——
    # 只有明细表有这个数的话，算费用就得回头扫明细，那正是分出日聚合表要避免的事
    cached_prompt_tokens = Column(
        BigInteger, default=0, nullable=False, comment="其中命中缓存的输入 token 合计"
    )
    # 下面两列是为报表加的：没有它们，「平均耗时」和「估算占比」就只能回头扫明细表，
    # 那正是分出日聚合表要避免的事。
    latency_ms_sum = Column(
        BigInteger, default=0, nullable=False, comment="耗时合计（毫秒），除以 requests 得均值"
    )
    estimated_requests = Column(
        Integer, default=0, nullable=False, comment="其中 token 为估算值的请求数"
    )
    estimated_tokens = Column(
        BigInteger, default=0, nullable=False, comment="其中估算得来的 token 合计"
    )
    # ---- 费用（按调用时单价累加，见 usage_records 的单价快照）----
    # 报表只查这张表，费用也必须能在这里直接取到，否则算钱又要回头扫明细。
    cost_sum = Column(
        Numeric(18, 6), default=0, nullable=False, comment="已定价请求的费用合计（按调用时单价）"
    )
    # 下面两个计数器决定这一行的费用**能不能用**，缺一不可：
    #   snapshot_requests < requests → 这一行混进了单价快照上线前的调用，
    #     合计是残缺的，整行退回按当前单价回算（跨越迁移当天的那一行会这样，
    #     第二天起自然恢复）
    #   priced_requests < snapshot_requests → 有调用当时压根没配单价，
    #     费用算不全 → 整组报 None，绝不按 0 混进总额
    snapshot_requests = Column(
        Integer, default=0, nullable=False, comment="其中带单价快照的请求数"
    )
    priced_requests = Column(
        Integer, default=0, nullable=False, comment="其中真正算出了费用的请求数"
    )
    cost_currency = Column(String(8), nullable=True, comment="费用货币")
    updated_at = Column(
        DateTime, default=now, onupdate=now, nullable=False, comment="更新时间"
    )


class CredentialAccessLog(Base):
    """凭证访问日志 —— 谁、何时、对谁的凭证做了什么"""

    __tablename__ = "credential_access_logs"
    __table_args__ = {"mysql_collate": "utf8mb4_general_ci"}

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="自增ID")
    operator_uid = Column(String(36), nullable=False, comment="操作者uid，系统自动操作填 system")
    target_user_uid = Column(String(36), nullable=False, comment="被访问凭证所属用户uid")
    platform = Column(String(32), nullable=False, comment="平台标识")
    action = Column(String(32), nullable=False, comment="动作 decrypt/create/update/delete")
    request_id = Column(String(64), nullable=True, comment="请求ID，串联访问日志")
    client_ip = Column(String(64), nullable=True, comment="来源IP")
    created_at = Column(DateTime, default=now, nullable=False, comment="发生时间")


# ==================== MCP 平台与细粒度授权 ====================


class McpInstance(Base, TimestampMixin):
    """一个可管理的 MCP 平台实例；密钥配置只以密文存在服务端。"""

    __tablename__ = "mcp_instances"
    __table_args__ = {"mysql_collate": "utf8mb4_general_ci"}

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(64), unique=True, nullable=False, comment="实例稳定编码，如 lingxing-main")
    name = Column(String(128), nullable=False)
    adapter_key = Column(String(64), nullable=False, index=True, comment="受信任适配器注册键")
    auth_config_encrypted = Column(Text, nullable=True, comment="认证配置 JSON（Fernet 密文）")
    config_json = Column(Text, nullable=True, comment="非敏感配置 JSON")
    status = Column(Integer, default=1, nullable=False)
    remark = Column(String(255), nullable=True)


class McpCapability(Base, TimestampMixin):
    """适配器同步出的只读能力目录。"""

    __tablename__ = "mcp_capabilities"
    __table_args__ = (
        UniqueConstraint("instance_id", "capability_code", name="uq_mcp_instance_capability"),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    instance_id = Column(Integer, nullable=False, index=True)
    capability_code = Column(String(128), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    domain = Column(String(64), nullable=False, index=True)
    description = Column(Text, nullable=True)
    method = Column(String(8), nullable=False)
    path = Column(String(512), nullable=False)
    input_schema_json = Column(Text, nullable=True)
    metadata_json = Column(Text, nullable=True)
    source_hash = Column(String(64), nullable=False, comment="适配器清单摘要，防止目录漂移")
    status = Column(Integer, default=1, nullable=False)


class McpUserPermission(Base, TimestampMixin):
    """用户对某实例下单个能力的授权；没有记录即拒绝。"""

    __tablename__ = "mcp_user_permissions"
    __table_args__ = (
        UniqueConstraint(
            "user_uid", "instance_id", "capability_id", name="uq_mcp_user_capability"
        ),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_uid = Column(String(36), nullable=False, index=True)
    instance_id = Column(Integer, nullable=False, index=True)
    capability_id = Column(Integer, nullable=False, index=True)
    effect = Column(String(8), default="allow", nullable=False, comment="allow/deny")
    constraints_json = Column(Text, nullable=True, comment="店铺、仓库、分页与时间范围约束")
    expires_at = Column(DateTime, nullable=True)
    status = Column(Integer, default=1, nullable=False)
    granted_by = Column(String(36), nullable=False)
    remark = Column(String(255), nullable=True)


class McpCallLog(Base):
    """MCP 调用审计；授权拒绝与执行失败同样留痕。"""

    __tablename__ = "mcp_call_logs"
    __table_args__ = {"mysql_collate": "utf8mb4_general_ci"}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    request_id = Column(String(64), nullable=False, index=True)
    user_uid = Column(String(36), nullable=False, index=True)
    api_key_id = Column(Integer, nullable=True)
    instance_id = Column(Integer, nullable=True, index=True)
    capability_id = Column(Integer, nullable=True)
    capability_code = Column(String(128), nullable=False, index=True)
    args_json = Column(Text, nullable=True, comment="已脱敏的请求参数")
    status = Column(String(16), nullable=False, index=True, comment="success/denied/failed")
    error_code = Column(String(64), nullable=True)
    upstream_request_id = Column(String(128), nullable=True, comment="上游链路ID，不含响应正文")
    latency_ms = Column(Integer, default=0, nullable=False)
    response_size = Column(BigInteger, default=0, nullable=False)
    created_at = Column(DateTime, default=now, nullable=False, index=True)


class ClientRelease(Base, TimestampMixin):
    """桌面端版本清单 —— 客户端只认这张表，不认 GitHub、不认上游官方源

    ## manifest_json 是唯一真源

    `manifest_json` 存的是**签名时那一份规范化清单**，其余业务列（version、
    platform、policy…）是它的冗余副本，只用于后台筛选、排序和展示。

    这样安排是因为签名覆盖的是清单的字节。如果按列存、读的时候再拼回一个
    dict，任何一处类型或格式的漂移——`artifact_size` 从 int 变成 str、
    `released_at` 被 DATETIME 列重新格式化成 `2026-08-24 02:30:00`——都会让
    重建出来的清单与签名对不上，表现为**全量客户端更新失败**，而库里每个字段
    看上去都是对的。存原文就没有这个问题。

    `released_at` 因此是 VARCHAR 而不是 DATETIME：它进了签名，必须逐字保留。
    它也是全库唯一一个 UTC 时间戳（其余走 model.database.now 的服务器本地时间），
    因为写它的是离线签名机，那台机器的时区不受我们控制。

    ## 网关签不出清单

    这张表里的 signature 只能由离线签名机产生。网关在发布接口上用公钥验一遍
    才准入库，它自己没有私钥。见 services/release_manifest.py 的模块说明。
    """

    __tablename__ = "client_releases"
    __table_args__ = (
        # 同一个版本对同一平台架构只能有一条：否则「哪一条才算数」没有答案
        UniqueConstraint("version", "platform", "arch", name="uq_client_release_target"),
        Index("ix_client_releases_lookup", "platform", "arch", "status"),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    manifest_json = Column(Text, nullable=False, comment="规范化清单原文，签名覆盖的就是它")
    signature = Column(Text, nullable=False, comment="离线签名机产出的 Ed25519 签名（base64）")
    public_key_fp = Column(String(32), nullable=False, comment="签名所用公钥指纹，便于确认信任根一致")

    # ---- 以下均为 manifest_json 的冗余副本，只用于筛选与后台展示 ----
    version = Column(String(32), nullable=False, index=True)
    platform = Column(String(16), nullable=False, comment="darwin/win32，与 Electron process.platform 同名")
    arch = Column(String(16), nullable=False, comment="x64/arm64")
    policy = Column(String(16), nullable=False, comment="optional/required")
    min_supported_version = Column(String(32), nullable=False, comment="低于此版本的客户端必须强制更新")
    artifact_url = Column(String(512), nullable=False)
    artifact_sha256 = Column(String(64), nullable=False)
    artifact_size = Column(BigInteger, nullable=False)
    released_at = Column(String(24), nullable=False, comment="UTC ISO，进签名，不可改格式")
    release_notes = Column(Text, nullable=True)

    status = Column(Integer, default=1, nullable=False, comment="1 上架 / 0 下架")
    published_by = Column(String(36), nullable=False, comment="发布人 uid")


class PromptPolicyDraft(Base, TimestampMixin):
    """全公司提示词策略的唯一草稿。

    草稿使用固定主键 ``1``。所有写操作都以 ``revision`` 做 CAS，避免两个后台
    页面互相覆盖；发布也会推进 revision，使发布前打开的旧页面不能继续静默保存。
    """

    __tablename__ = "prompt_policy_drafts"
    __table_args__ = {"mysql_collate": "utf8mb4_general_ci"}

    id = Column(Integer, primary_key=True, autoincrement=False, comment="固定为 1")
    company_core_policy = Column(
        Text().with_variant(MEDIUMTEXT, "mysql"),
        nullable=False,
        default="",
        comment="不可覆盖的公司核心规则草稿",
    )
    default_persona = Column(
        Text().with_variant(MEDIUMTEXT, "mysql"),
        nullable=False,
        default="",
        comment="默认人格草稿",
    )
    content_hash = Column(String(71), nullable=True, comment="规范化策略包 sha256")
    revision = Column(Integer, nullable=False, default=0, comment="草稿乐观锁版本")
    base_version = Column(BigInteger, nullable=True, comment="草稿最近一次发布形成的版本")
    updated_by_uid = Column(String(36), nullable=True)
    updated_by_name = Column(String(64), nullable=True)


class PromptPolicyVersion(Base):
    """已发布提示词策略的不可变快照；当前版本永远是最大 version。"""

    __tablename__ = "prompt_policy_versions"
    __table_args__ = (
        Index("ix_prompt_policy_versions_hash", "content_hash"),
        Index("ix_prompt_policy_versions_published", "published_at"),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    version = Column(BigInteger, primary_key=True, autoincrement=False)
    company_core_policy = Column(
        Text().with_variant(MEDIUMTEXT, "mysql"), nullable=False
    )
    default_persona = Column(
        Text().with_variant(MEDIUMTEXT, "mysql"), nullable=False
    )
    content_hash = Column(String(71), nullable=False)
    # 普通发布为空；只有回滚生成的版本指向被恢复的历史版本。
    source_version = Column(BigInteger, nullable=True)
    published_at = Column(DateTime, default=now, nullable=False)
    published_by_uid = Column(String(36), nullable=False)
    published_by_name = Column(String(64), nullable=False)


class PromptPolicyAuditLog(Base):
    """提示词策略操作审计。

    故意不存正文：审计回答的是谁在何时把哪个 hash 变成了什么版本，正文只存在
    草稿与不可变版本表中，避免普通日志与审计查询再复制一份敏感规则。
    """

    __tablename__ = "prompt_policy_audit_logs"
    __table_args__ = (
        Index("ix_prompt_policy_audit_created", "created_at"),
        Index("ix_prompt_policy_audit_action", "action"),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    action = Column(
        String(32), nullable=False,
        comment="draft_created/draft_updated/published/rolled_back",
    )
    actor_uid = Column(String(36), nullable=False)
    actor_name = Column(String(64), nullable=False)
    content_hash = Column(String(71), nullable=False)
    draft_revision = Column(Integer, nullable=True)
    version = Column(BigInteger, nullable=True)
    source_version = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=now, nullable=False)


class SkillSecuritySample(Base):
    """被隔离技能的样本本体 —— 阶段 7 · S7-C

    **按内容去重，不按人去重**。同一个坏技能在五十台机器上被隔离，样本只存一份，
    五十条事件都指向它。反过来按事件存的话，一次 hub 上的问题技能扩散就能让这张
    表在一天里涨到几百 MB，而里面全是同一份内容。

    ⚠️ files_json 必须是 MEDIUMTEXT，不能是 TEXT

    TEXT 只有 64KB。客户端的样本上限是 512KB（见 tools/skill_report.py），
    塞进 TEXT 列的后果是 MySQL 非严格模式下**静默截断** —— 库里那一行看上去
    好好的，审查的人读到的却是半截代码，而恰恰是「后面被截掉的部分」最可能是
    藏东西的地方。接收接口另有一道体积检查，两道都留着：ORM 这一层保证列放得下，
    接口那一层保证不会有人塞进来一个 50MB 的东西。
    """

    __tablename__ = "skill_security_samples"
    __table_args__ = (
        UniqueConstraint("sample_hash", name="uq_skill_sample_hash"),
        Index("ix_skill_samples_bundle", "bundle_hash"),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="自增ID")
    # 服务端按「文件路径 + 各自 sha256」算出来的内容指纹。不用客户端上报的
    # bundle_hash 作为去重键：那个字段可能为空（隔离时没传），空值当键会把
    # 互不相干的样本合成一条。
    sample_hash = Column(String(64), nullable=False, comment="样本内容指纹，去重键")
    bundle_hash = Column(String(64), nullable=True, comment="客户端算的技能包哈希，用于与事件互相串联")

    file_count = Column(Integer, default=0, nullable=False, comment="技能包里的文件总数")
    included_count = Column(Integer, default=0, nullable=False, comment="带内容上传的文件数")
    included_bytes = Column(BigInteger, default=0, nullable=False, comment="带内容上传的字节数")
    truncated = Column(Boolean, default=False, nullable=False, comment="是否有文件因超限只留了元数据")
    truncated_reason = Column(String(32), nullable=True, comment="file_count/total_bytes")
    files_json = Column(
        Text().with_variant(MEDIUMTEXT, "mysql"),
        nullable=False,
        comment="文件清单与内容原文 JSON（超限的只有元数据）",
    )

    seen_count = Column(Integer, default=1, nullable=False, comment="这份样本被上报过多少次")
    first_seen_at = Column(DateTime, default=now, nullable=False, comment="首次上报时间")
    last_seen_at = Column(DateTime, default=now, nullable=False, comment="最近一次上报时间")


class SkillSecurityEvent(Base):
    """技能隔离、自助恢复与发布扫描提醒的留痕 —— 阶段 7 · S7-C

    产品前提：技能来源不封禁（自有 skill hub 是给部门技能指标用的）。所以这张表
    不是「拦截记录」，是**越过记录** —— 员工可以把被判定为危险的技能放回去，
    代价是这个动作在这里留下一行。

    ## 身份不听客户端的

    user_uid / dept_id 来自网关对 API Key 的解析，不取客户端上报的字段。客户端
    跑在员工自己的机器上，它说自己是谁没有意义。`device` 是客户端给的，仅供人工
    定位「去哪台机器上看」，不参与任何判定。

    ## event_id 是幂等键

    客户端发不出去就重试，网络超时那一类失败里「其实已经写进去了」是常态。
    没有幂等键的话，一次网关抖动就能让同一条隔离在库里变成五行，而审查的人
    无从判断这是重传还是真的发生了五次。

    ## occurred_at 是 VARCHAR

    客户端 UTC 字符串原样存。跟 client_releases.released_at 同一个理由：写它的
    机器时区不受我们控制，转成 DATETIME 只会引入「这一列到底是哪个时区」的歧义。
    排序和报表用 created_at（服务器本地时间，全库统一口径）。
    """

    __tablename__ = "skill_security_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_skill_event_id"),
        Index("ix_skill_events_user", "user_uid", "created_at"),
        Index("ix_skill_events_dept", "dept_id", "created_at"),
        Index("ix_skill_events_kind", "event", "created_at"),
        Index("ix_skill_events_bundle", "bundle_hash"),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="自增ID")
    event_id = Column(String(64), nullable=False, comment="客户端生成的事件ID，幂等键")
    event = Column(String(32), nullable=False, comment="skill_quarantined / skill_quarantine_restored")

    # ---- 身份快照：来自 API Key，不是客户端上报 ----
    user_uid = Column(String(36), nullable=False, comment="上报人uid（网关解出来的）")
    dept_id = Column(Integer, nullable=True, comment="上报时所属部门（快照，不随调岗变）")
    dept_code = Column(String(64), nullable=True, comment="上报时部门代码（快照）")
    device = Column(String(128), nullable=True, comment="客户端自报的机器标签，仅供人工定位")

    skill_name = Column(String(255), nullable=False, comment="技能名")
    entry_id = Column(String(64), nullable=True, comment="客户端隔离条目ID，同机上串联隔离与恢复")
    verdict = Column(String(16), nullable=True, comment="扫描结论 dangerous/caution/safe")
    reason = Column(String(512), nullable=True, comment="隔离原因（扫描器给的摘要）")
    source = Column(String(64), nullable=True, comment="技能来源")
    origin_relpath = Column(String(512), nullable=True, comment="技能在活动树里的相对路径")

    scanner_version = Column(String(32), nullable=True, comment="扫描器版本，结论随它变")
    bundle_hash = Column(String(64), nullable=True, index=True, comment="技能包哈希，串联隔离与恢复两条事件")
    finding_count = Column(Integer, default=0, nullable=False, comment="命中项数量")
    findings_json = Column(Text, nullable=True, comment="命中项明细 JSON")

    sample_id = Column(
        BigInteger, nullable=True, index=True, comment="样本ID；恢复事件为空（样本隔离时已传过）"
    )

    occurred_at = Column(String(24), nullable=True, comment="客户端本地记录的发生时间（UTC ISO 原文）")
    reported_at = Column(String(24), nullable=True, comment="客户端入队时间（UTC ISO 原文）")
    schema_version = Column(Integer, default=1, nullable=False, comment="客户端载荷版本")
    created_at = Column(DateTime, default=now, nullable=False, index=True, comment="网关收到的时间")


class SkillHubUsageEvent(Base):
    """工作台实际加载技能的去重事件；统计不参与权限或计费。"""
    __tablename__ = "skill_hub_usage_events"
    __table_args__ = (
        UniqueConstraint("user_uid", "event_id", name="uq_skill_usage_user_event"),
        Index("ix_skill_usage_slug", "slug"),
        {"mysql_collate": "utf8mb4_general_ci"},
    )
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    user_uid = Column(String(36), nullable=False)
    event_id = Column(String(32), nullable=False)
    slug = Column(String(64), nullable=False)
    api_key_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=now, nullable=False)


class SkillHubSkill(Base, TimestampMixin):
    """自有 skill hub 里一个技能的**身份** —— 阶段 11 · S11-A

    产品前提（2026-08-26 负责人定，见 TODO 阶段 7 开头）：hub 存在的理由是给各
    部门下技能指标 —— 每月做多少 skill、总结多少工作经验，把员工的工作经验转成
    公司资产。所以这张表的重点不只是「有哪些技能」，还有**是谁发的、算谁的**。

    ## 身份与版本分成两张表

    技能会迭代。把内容和身份塞进一张表的话，发第二版要么覆盖第一版（历史没了，
    出事回不去），要么插一行新的（slug 不能唯一，安装时不知道该装哪个）。
    分开之后 slug 唯一约束落在这张表，版本各自成行，`latest_version_id` 指向当前
    该装的那个。

    ## status 是「先发后审」

    发布即全公司可见，管理员事后可以下架。这跟技能安全那一套是同一个取向：
    不拦人、留痕、事后处理。审核队列会卡住技能指标，而指标正是这东西存在的理由。

    下架不删数据 —— 已经装到员工机器上的那些不会因为下架而消失，留着行才能回答
    「这个技能当时是谁发的、为什么被下掉」。

    ## owner 是快照，不随调岗变

    与 skill_security_events 同一个理由：统计的是「发布当时算谁的部门指标」。
    人调岗之后回头改历史归属，会让上个月已经报出去的部门数据对不上。
    """

    __tablename__ = "skill_hub_skills"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_skill_hub_slug"),
        Index("ix_skill_hub_owner", "owner_uid", "created_at"),
        Index("ix_skill_hub_dept", "owner_dept_id", "created_at"),
        Index("ix_skill_hub_status", "status", "updated_at"),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="自增ID")
    # 安装标识。客户端 SkillMeta.identifier 用的就是它，也是技能装到磁盘上的目录名，
    # 所以字符集必须比「显示名」严格得多（见 services/skill_hub.normalize_slug）。
    slug = Column(String(64), nullable=False, comment="安装标识，全公司唯一")
    name = Column(String(255), nullable=False, comment="显示名")
    description = Column(String(1024), nullable=False, default="", comment="一句话说明")

    latest_version_id = Column(BigInteger, nullable=True, comment="当前该装的版本ID")
    latest_version = Column(Integer, default=0, nullable=False, comment="当前版本号，从1递增")

    # ---- 归属快照：来自 API Key，不是客户端上报 ----
    owner_uid = Column(String(36), nullable=False, comment="首发人uid（网关解出来的）")
    owner_username = Column(String(64), nullable=True, comment="首发人用户名快照，后台列表直接显示")
    owner_dept_id = Column(Integer, nullable=True, comment="首发时所属部门（快照，不随调岗变）")
    owner_dept_code = Column(String(64), nullable=True, comment="首发时部门代码（快照）")

    # ---- 空间分层（阶段 21，负责人 2026-08-31 定）----
    #
    # 只有**两级**：部门空间与公司空间。个人技能留在客户端本地，**不上传列表** ——
    # 那会让「员工装了哪些技能」变成网关上的数据，而这件事没有非做不可的理由。
    # 客户端发起的是「发布到部门空间」的请求，不是同步。
    #
    # 晋级到公司空间由**主管**决定，没有管理部门审核那一步：管理部门基本不上线看，
    # 而且技能已经过主管审查了。出问题由主管撤回、或管理部门登录后台下架。
    # 多一道没人处理的审批队列，等于把技能永远卡在部门里。
    #
    # ⚠️ 2026-09-03 起 scope **不再决定可见性**：已上架的技能对所有人可见
    # （见 db/selects/skill_hub._visible_to）。晋级那道关卡在发布初期卡得太死 ——
    # 技能做出来得等主管点一下才有第二个人看得见，而主管多数时候不知道有东西
    # 在等他。现在 company 读作「公司精选」，只影响排序；dept 是分组维度。
    scope = Column(
        String(16), default="dept", nullable=False,
        comment="dept=部门空间 / company=公司空间；没有 personal（个人技能不上服务端）",
    )
    # 部门空间的归属。**与 owner_dept_id 分开存**：owner_dept_id 是「首发人当时在
    # 哪个部门」的快照，用于技能指标；这个是「这个技能现在属于哪个部门空间」，
    # 主管可以把它晋级或退回，两者会分叉。
    scope_dept_id = Column(Integer, nullable=True, index=True, comment="所属部门空间；company 时为空")
    promoted_by_uid = Column(String(36), nullable=True, comment="把它发到公司空间的人")
    promoted_at = Column(DateTime, nullable=True, comment="晋级时间")

    status = Column(String(16), default="listed", nullable=False, comment="listed / delisted")
    delisted_reason = Column(String(512), nullable=True, comment="下架原因，员工搜不到时后台能答上来")
    delisted_by_uid = Column(String(36), nullable=True, comment="下架操作人uid")
    delisted_at = Column(DateTime, nullable=True, comment="下架时间")

    install_count = Column(Integer, default=0, nullable=False, comment="被取包次数，粗略的热度")


class SkillHubVersion(Base):
    """一个技能某一版的**内容本体** —— 阶段 11 · S11-A

    ## 版本号是递增整数，不是 semver

    内部 hub 的发布者是同事，不是上游维护者，没人会认真维护语义化版本。给一个
    自己管不住的字段（"1.0.0" 之后填什么全凭心情）不如给一个服务端说了算的
    计数器：`latest_version + 1`。要看改了什么就读 changelog。

    ## bundle_hash 是「这一版是不是真的变了」

    按「文件路径 + 各自 sha256」算，服务端算不听客户端的（同 skill_security_samples）。
    同一个技能重复发布同样的内容时不新增版本，直接返回已有那版 —— 否则员工每按一次
    发布就多一版，版本号变成按钮点击次数。

    ⚠️ files_json 必须是 MEDIUMTEXT，不能是 TEXT

    与 skill_security_samples.files_json 同一个理由，但**后果更重**：那张表存的是
    给人看的取证样本，截断了是读到半截代码；这张表存的是**会被装到几十台机器上
    执行的东西**，MySQL 非严格模式下静默截断意味着分发出去的是残包，而客户端拿到
    的 JSON 会直接解析失败——员工看到的是「装不上」，离真正的原因隔着好几层。
    接收接口另有一道体积检查，两道都留着。
    """

    __tablename__ = "skill_hub_versions"
    __table_args__ = (
        UniqueConstraint("skill_id", "version", name="uq_skill_hub_version"),
        UniqueConstraint("skill_id", "bundle_hash", name="uq_skill_hub_bundle"),
        Index("ix_skill_hub_ver_publisher", "publisher_uid", "created_at"),
        Index("ix_skill_hub_ver_dept", "publisher_dept_id", "created_at"),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="自增ID")
    skill_id = Column(BigInteger, nullable=False, index=True, comment="所属技能ID")
    version = Column(Integer, nullable=False, comment="版本号，服务端从1递增")

    bundle_hash = Column(String(64), nullable=False, comment="内容指纹（服务端算），同内容不新增版本")
    files_json = Column(
        Text().with_variant(MEDIUMTEXT, "mysql"),
        nullable=False,
        comment="文件清单与内容 JSON，逐文件带 utf-8/base64 编码信封",
    )
    file_count = Column(Integer, default=0, nullable=False, comment="文件数")
    total_bytes = Column(BigInteger, default=0, nullable=False, comment="解码后的原始字节数")

    changelog = Column(String(1024), nullable=True, comment="这一版改了什么，发布者填")

    # ---- 发布者快照：每版各自记，技能可能由不同人迭代 ----
    publisher_uid = Column(String(36), nullable=False, comment="发布人uid（网关解出来的）")
    publisher_username = Column(String(64), nullable=True, comment="发布人用户名快照")
    publisher_dept_id = Column(Integer, nullable=True, comment="发布时所属部门（快照）")
    publisher_dept_code = Column(String(64), nullable=True, comment="发布时部门代码（快照）")

    created_at = Column(DateTime, default=now, nullable=False, index=True, comment="发布时间")


# ==================== 协作（项目群聊）—— 阶段 13 ====================
#
# 设计决议见 TODO「协作（群聊）功能设计决议」整章。这里只重复三条会直接影响
# 建模、且**事后补不了**的：
#
# - **P0-5：禁止「全局唯一 = 公司唯一」的隐含假设。** 不提前建多租户（过度设计），
#   但新表一律带项目作用域字段。toB 时客户没有我们的中台账号，决策 #45「能力不
#   跨界」同样是那时唯一可行的模型 —— 两件事同一个解法。
# - **P0-4：项目记忆与个人 agent 记忆必须从一开始就分开存。** 这是离职处理的前置
#   条件。混在一个存储里，离职当天只剩两个都错的选项：连人带项目知识一起带走，
#   或者把个人数据强留在公司。
# - **P0-3：审计模型要从一对一扩到多方。** conversation_logs 是单用户单会话结构。
#   「这不是加个 group_id 字段能了事的」—— 建模前先想清楚事后要回答什么问题，
#   见 ProjectMessage 的说明。


class Project(Base, TimestampMixin):
    """一个协作项目 —— 阶段 13

    ## code 是作用域键，不是显示名

    下游每张表都带 `project_id`。`code` 是给人和 URL 用的稳定标识，创建后不可改 ——
    改了等于换了一个项目，而群聊历史、任务、仓库都还挂在旧的上。

    ## owner_uid 与「离职不转让」（决策 #46）

    项目有负责人，但**负责人离职不触发 agent 转让**。转让等于顶着离职员工身份调
    数据，直接违反决策 #43。要做的是「项目角色空缺 → 重新分配」：新负责人用自己的
    账号和自己的 agent 接管，项目侧知识由项目记忆提供。所以这里存的是「当前是谁」，
    换人就是改这个字段，没有任何「继承」语义。

    ## 仓库异步创建，项目本身不依赖 Gitea 可用性

    决策 #47 定的是自建 Gitea/Forgejo（选型动因是 gitee 私有仓的**协作者人数额度**，
    不是数据自控 —— 这一点要记准，否则后面会基于错误理由做错误推论）。实例已经跑在
    `https://git.example.invalid/`。本地项目先提交，再异步、幂等地建远端仓库；
    Gitea 故障不能让项目创建回滚。pending / provisioning / failed / ready 是显式
    状态，避免客户端把空 URL 当成「没有仓库功能」。
    """

    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("code", name="uq_project_code"),
        Index("ix_projects_owner", "owner_uid"),
        Index("ix_projects_status", "status", "updated_at"),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="自增ID")
    code = Column(String(64), nullable=False, comment="项目标识，创建后不可改")
    name = Column(String(255), nullable=False, comment="显示名")
    description = Column(String(1024), nullable=False, default="", comment="一句话说明")

    owner_uid = Column(String(36), nullable=False, comment="当前负责人uid（换人=改这里，无继承语义）")
    created_by_uid = Column(String(36), nullable=False, comment="创建人uid（不随换人变）")
    # 快照，与 usage_records 同一理由：调岗、改名之后历史不该跟着变。
    created_dept_id = Column(Integer, nullable=True, comment="创建时所属部门（快照）")
    created_dept_code = Column(String(64), nullable=True, comment="创建时部门代码（快照）")

    status = Column(String(16), default="active", nullable=False, comment="active / archived")
    archived_at = Column(DateTime, nullable=True, comment="归档时间")
    archived_reason = Column(String(512), nullable=True, comment="归档原因")

    git_repo_url = Column(String(512), nullable=True, comment="自建 Gitea 仓库地址，对接前为空")
    git_repo_id = Column(BigInteger, nullable=True, comment="Gitea 仓库 ID")
    git_repo_name = Column(String(128), nullable=True, comment="稳定的 Gitea 仓库名")
    git_repo_status = Column(
        String(16), default="pending", nullable=False, index=True,
        comment="pending / provisioning / failed / ready",
    )
    git_repo_error = Column(String(512), nullable=True, comment="最近一次建仓失败摘要")
    git_repo_attempted_at = Column(DateTime, nullable=True, comment="最近一次建仓尝试时间")

    # 消息序号的发号器。放在项目行上而不是靠 max(seq)+1 查：并发发言时后者必然撞。
    message_seq = Column(BigInteger, default=0, nullable=False, comment="项目内消息序号水位")


class ProjectMember(Base, TimestampMixin):
    """项目成员 —— 可以是人，也可以是某个人的 agent

    ## agent 是**一等成员**，而不是「人的一个属性」

    "用户可把自己的 HermesAgent 拉进群"。把 agent 记成人的一个布尔字段的话，
    「这条消息是谁说的」就答不清楚 —— 而 P0-3 要求的正是这个。所以 member_kind
    区分 user / agent，agent 的 `user_uid` 存**主人的 uid**。

    归属必须显式：群聊触发的调用要挂主人 `user_uid` 记账（否则用量报表口径直接
    断掉），而「哪个 agent 是谁的」就是从这里查的。

    ## 退出不删行

    "被移除/退出时 agent 自动退出 —— **成立，但远不够**：退群只解决可见性。
    真正要撤的是该 agent 对项目上下文与仓库的访问；且**已流入群聊历史的内容
    收不回来**，退出不能当补救手段用。"

    所以这里是 status + left_at，不是 DELETE：历史发言还挂在这个成员上，
    删了行之后那些发言就没有主语了。
    """

    __tablename__ = "project_members"
    __table_args__ = (
        # 同一个项目里，一个人只能有一条人成员记录；同一个人的同名 agent 也只有一条。
        UniqueConstraint("project_id", "member_kind", "user_uid", "agent_label",
                         name="uq_project_member"),
        Index("ix_project_members_uid", "user_uid", "status"),
        Index("ix_project_members_project", "project_id", "status"),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="自增ID")
    project_id = Column(BigInteger, nullable=False, index=True, comment="所属项目")

    member_kind = Column(String(8), nullable=False, comment="user / agent")
    # agent 成员这里存的是**主人的 uid** —— 记账与归属都靠它。
    user_uid = Column(String(36), nullable=False, comment="人成员=本人uid；agent 成员=主人uid")
    username = Column(String(64), nullable=True, comment="用户名快照，列表直接显示")
    agent_label = Column(String(64), nullable=False, default="", comment="agent 的显示名；人成员为空串")

    dept_id = Column(Integer, nullable=True, comment="加入时所属部门（快照）")
    dept_code = Column(String(64), nullable=True, comment="加入时部门代码（快照）")

    role = Column(String(16), default="member", nullable=False, comment="owner / member / viewer")

    # ---- 谁可以指挥这个 agent（阶段 27，P0-D）----
    #
    # 只对 agent 成员有意义；人成员上恒为默认值。判定在服务端做
    # （services/project_runtime.may_trigger），桌面端的选择器只是配置与展示。
    #
    # 默认 project_members 而不是更严的一档：这里允许的只是一个**无能力的群聊
    # 回复**（没有 MCP、skill、个人记忆和主人凭据），而默认太严的直接后果是
    # 「@ 了不回」被当成故障，然后每个人都把自己的 agent 调到最松。
    respond_to = Column(
        String(16), default="project_members", nullable=False,
        comment="owner_only / allowlist / project_members / nobody；仅 agent 成员有意义",
    )
    # allowlist 档的名单，存主人指定的成员 uid（JSON 数组）。只在本项目内有意义。
    respond_allowlist = Column(
        Text, nullable=True, comment="respond_to=allowlist 时的成员 uid 列表（JSON 数组）"
    )

    status = Column(String(16), default="active", nullable=False, comment="active / left")
    joined_at = Column(DateTime, default=now, nullable=False, comment="加入时间")
    left_at = Column(DateTime, nullable=True, comment="退出/被移除时间")
    left_reason = Column(String(255), nullable=True, comment="退出原因，供事后追")


class ProjectMessage(Base):
    """项目群聊的一条消息 —— P0-3 的多方审计模型

    ## 建模前先想清楚「事后要回答什么问题」

    参考会话审计那次的教训（列表与详情拆开，查阅日志才记得住具体看了哪一条）。
    这张表要能回答的是：

    1. **谁说的** —— 人还是 agent；agent 的话算谁的（`sender_uid` 是主人）
    2. **谁看得见** —— 目前是项目成员范围，字段留 `visibility` 以便将来收窄
    3. **哪条消息触发了哪次出网调用** —— `trigger_request_id` 关联 usage_records
       与 mcp_call_logs 的 request_id
    4. **agent 的回复归属谁** —— 同 1，`sender_uid` 就是主人
    5. **回复的是哪一条** —— `reply_to_id`

    只加一个 group_id 的话，1、3、4 全都答不上来。

    ## 原文与脱敏后**都存**，投递出去的只有脱敏后那份

    P0-2：现有脱敏（决策 #9/#37）只覆盖「发往模型上游」这一个方向。群聊里发给
    **其他部门 / 其他公司的人**同样是内容外流，必须走同一条替换管道，否则关键词
    防线在协作场景下直接失效 —— 而且失效得很安静。

    与会话审计的双通道是同一个结构：审计留原文，出去的是脱敏后。**注意方向不同**
    —— 会话那边脱敏是为了不让关键词到达模型厂商，这边是为了不让它到达别的人，
    但用的必须是同一份关键词表，否则两条路会各自漂移。

    ## seq 是项目内序号，不是全局自增 id

    「读到哪了」「从第 N 条之后拉」这类查询要一个项目内单调的数。用全局 id 的话，
    项目 A 的读游标会因为项目 B 的写入而跳号，看起来像丢了消息。
    """

    __tablename__ = "project_messages"
    __table_args__ = (
        UniqueConstraint("project_id", "seq", name="uq_project_message_seq"),
        Index("ix_project_messages_sender", "sender_uid", "created_at"),
        Index("ix_project_messages_trigger", "trigger_request_id"),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="自增ID")
    project_id = Column(BigInteger, nullable=False, index=True, comment="所属项目")
    seq = Column(BigInteger, nullable=False, comment="项目内序号，读游标用")

    sender_kind = Column(String(8), nullable=False, comment="user / agent / system")
    sender_uid = Column(String(36), nullable=False, comment="人=本人；agent=**主人**uid（归属与记账）")
    sender_username = Column(String(64), nullable=True, comment="用户名快照")
    sender_agent_label = Column(String(64), nullable=True, comment="agent 显示名；人发言为空")
    sender_dept_id = Column(Integer, nullable=True, comment="发言时所属部门（快照）")
    sender_dept_code = Column(String(64), nullable=True, comment="发言时部门代码（快照）")

    reply_to_id = Column(BigInteger, nullable=True, comment="回复的哪一条")
    mentions_json = Column(Text, nullable=True, comment="被 @ 的成员，决定强制触发")

    # 投递出去的那份。读接口只给这个。
    content = Column(
        Text().with_variant(MEDIUMTEXT, "mysql"), nullable=False, comment="脱敏后正文（投递给成员的）"
    )
    # 审计用原文。⚠️ 与 conversation_logs 的原文同级敏感，受同一套留存期管辖。
    content_original = Column(
        Text().with_variant(MEDIUMTEXT, "mysql"), nullable=True, comment="脱敏前原文（仅审计可见）"
    )
    redaction_hits = Column(Integer, default=0, nullable=False, comment="命中关键词数，0 表示两份相同")

    visibility = Column(String(16), default="project", nullable=False, comment="project；留待将来收窄")
    trigger_request_id = Column(
        String(64), nullable=True, comment="这条消息触发的出网调用 request_id，串 usage/mcp 日志"
    )

    created_at = Column(DateTime, default=now, nullable=False, index=True, comment="服务器本地时间")


class ProjectTask(Base, TimestampMixin):
    """项目任务 —— P0-6：认领会把污染从群聊带回私聊

    ## 这张表存在的首要理由不是「管理任务」，是**挡住一条绕道**

    决策 #45 把能力挡在群聊之外（阶段 12 已落地），但攻击路径可以绕：
    **群里发一个夹带注入的任务描述 → agent 认领 → 回到私聊上下文执行 → 此时它
    持有主人的全部 MCP 凭据**。执行不发生在群聊，群聊禁用能力就完全落空。

    所以：

    - `source_kind` / `source_message_id` 记下这段文字**是从哪来的**
    - `untrusted` 是不可信标记，**跟着任务描述一路进入私聊上下文**，不因为
      「这是主人自己的会话」就恢复完全信任
    - `claim_state` 把「认领」和「开工」拆成两步：**认领 ≠ 自动开工**，
      认领后需主人显式确认才进入执行

    最后一条是落地对策的核心。「人在环这一下比任何自动检测都可靠，成本也极低」——
    确认时把任务原文**原样**呈给他看，注入的那句话就摆在他眼前。

    ## untrusted 默认为真，只有一条路能让它为假

    群里来的一律不可信。只有**项目成员在私聊里自己写的**任务才是可信的 ——
    而那种情况下他本来就在读自己写的东西。默认值写在这一侧，是因为反过来
    （默认可信、遇到群聊来源才标脏）漏一处就等于这条防线不存在。

    ## 认领人与执行人是同一个人，但记两次

    `claimed_by_uid` 是认领的人，`claimed_agent_label` 是他打算用哪个 agent 干。
    停用账号时「已认领但未完成的任务要自动退回未认领池并 @ 项目负责人」（决策 #46）
    查的就是前者 —— 否则任务永久卡在一个停用账号名下。
    """

    __tablename__ = "project_tasks"
    __table_args__ = (
        UniqueConstraint("project_id", "seq", name="uq_project_task_seq"),
        Index("ix_project_tasks_project", "project_id", "status"),
        Index("ix_project_tasks_claimer", "claimed_by_uid", "status"),
        Index("ix_project_tasks_source", "source_message_id"),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="自增ID")
    project_id = Column(BigInteger, nullable=False, index=True, comment="所属项目")
    seq = Column(Integer, nullable=False, comment="项目内任务号，给人念的")

    title = Column(String(255), nullable=False, comment="标题")
    # ⚠️ 正文是**不可信输入**。它会被原样呈给认领人看（那正是防线所在），
    # 但绝不能在任何地方被当作指令直接执行。
    body = Column(Text, nullable=True, comment="任务描述（不可信输入，原样保留）")

    # ---- 来源：P0-6 的核心 ----
    source_kind = Column(
        String(16), default="chat", nullable=False,
        comment="chat=群聊消息 / manual=成员在项目页手写；决定 untrusted 的默认值",
    )
    source_message_id = Column(BigInteger, nullable=True, comment="来自哪条群聊消息")
    # 默认 True：群里来的一律不可信。反过来（默认可信、遇到群聊才标脏）
    # 漏一处就等于这条防线不存在。
    untrusted = Column(
        Boolean, default=True, nullable=False,
        comment="不可信标记，跟着任务描述进入私聊上下文，不因为『这是主人的会话』而清除",
    )

    created_by_uid = Column(String(36), nullable=False, comment="创建人uid（网关解出来的）")
    created_by_username = Column(String(64), nullable=True, comment="创建人用户名快照")

    # ---- 认领与开工是**两步** ----
    status = Column(
        String(16), default="open", nullable=False,
        comment="open=未认领 / claimed=已认领待确认 / active=已开工 / done / cancelled",
    )
    claimed_by_uid = Column(String(36), nullable=True, comment="认领人uid；停用账号时按它退回")
    claimed_by_username = Column(String(64), nullable=True, comment="认领人用户名快照")
    claimed_agent_label = Column(String(64), nullable=True, comment="打算用哪个 agent 干")
    claimed_at = Column(DateTime, nullable=True, comment="认领时间")
    # 主人显式确认的那一下。没有它就是 claimed 而不是 active ——
    # 「认领 ≠ 自动开工」这条规则的落点就是这个字段。
    confirmed_at = Column(DateTime, nullable=True, comment="主人确认开工的时间")
    result_message_id = Column(BigInteger, nullable=True, comment="经预览发布的项目结果消息")
    done_at = Column(DateTime, nullable=True, comment="完成时间")
    returned_count = Column(Integer, default=0, nullable=False, comment="被退回未认领池几次")


class ProjectAgentTurn(Base, TimestampMixin):
    """群聊 agent 的一次执行租约 —— 阶段 27（P0-C / P0-E）

    ## 为什么执行域必须落在服务端的一张表上

    每个 agent 跑在**它主人自己的机器上**。「同一个项目同时最多一个 in-flight
    turn」是跨机器约束：A 的 sidecar 不可能知道 B 的 sidecar 正在回同一个项目。
    放在客户端做的话，两台机器同时开工是必然而不是偶发，群里就会出现两个 agent
    抢答同一条消息。

    所以开工前先来这里拿租约：`uq_project_inflight` 那条唯一索引就是那把锁。

    ## 它同时是**身份**的来源

    在此之前「agent 在群里发言」只能长成「客户端说自己是 agent 就当 agent」——
    决策 #45 反复说不能做的那种设计。有了租约，写回时身份从租约取
    （services/project_runtime.writeback_identity）：project、agent、主人 uid、
    触发事件和 `trigger_request_id` 全在服务端发租约的那一刻就锁死了，
    客户端连 agent_label 都不用报，也就没有可伪造的字段。

    ## 租约会过期

    客户端崩溃、断网、关机时 turn 不会有人来收尾。没有 `lease_expires_at` 的话，
    那个项目就永久卡在「有人在跑」上，而且没有任何报错。续租失败即视为死亡，
    项目继续。

    ## 跳数记在项目上，不是记在这一行上

    连续 agent-to-agent 上限（默认 8）按项目累计，人一说话就清零 —— 所以
    `hop_index` 只是这一跳的快照，用来事后回答「第几跳停的」。
    """

    __tablename__ = "project_agent_turns"
    __table_args__ = (
        # 一个项目同时只有一个未结束的 turn。做成部分唯一索引在 MySQL 里没有，
        # 所以用 `inflight_key`：未结束时写 project_id，进终态时置空（见迁移注释）。
        UniqueConstraint("inflight_key", name="uq_project_inflight"),
        Index("ix_project_turns_project", "project_id", "status"),
        Index("ix_project_turns_owner", "owner_uid", "status"),
        Index("ix_project_turns_trigger", "trigger_request_id"),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="自增ID")
    project_id = Column(BigInteger, nullable=False, index=True, comment="所属项目")
    # 未结束时 = project_id；进终态时置 NULL。MySQL 的唯一索引允许多个 NULL，
    # 这正好表达「已结束的 turn 想有多少有多少，在跑的只能有一个」。
    inflight_key = Column(BigInteger, nullable=True, comment="在跑时=project_id，结束置空")

    member_id = Column(BigInteger, nullable=False, comment="哪个 agent 成员（project_members.id）")
    agent_label = Column(String(64), nullable=False, default="", comment="agent 显示名，写回用")
    # agent 的发言归属主人：记账口径与责任都靠它。
    owner_uid = Column(String(36), nullable=False, comment="agent 主人的 uid，也是记账对象")

    # ---- 触发来源：审计要能逐笔对账 ----
    trigger_reason = Column(
        String(16), nullable=False,
        comment="mention / task_assigned / task_event / owner_request；P0 只有这四种",
    )
    trigger_seq = Column(BigInteger, nullable=False, comment="触发事件在项目内的 seq")
    trigger_message_id = Column(BigInteger, nullable=True, comment="触发的群聊消息")
    trigger_task_id = Column(BigInteger, nullable=True, comment="触发的任务")
    requester_uid = Column(String(36), nullable=False, comment="谁触发的（权限判定的依据）")
    # 串起项目消息、usage 与相关拒绝日志。发租约时就生成，不等模型调用。
    trigger_request_id = Column(String(64), nullable=False, comment="串 usage/拒绝日志的 request_id")
    # 一批里其余事件的 id + seq + 发送者 + 原因。合成上下文可以，但审计关联不能合掉。
    batch_json = Column(Text, nullable=True, comment="同批其它触发事件的快照（JSON）")

    hop_index = Column(Integer, default=0, nullable=False, comment="这一跳是连续 agent 对话的第几跳")

    status = Column(
        String(16), default="queued", nullable=False,
        comment="queued / running / completed / failed / cancelled",
    )
    # 状态水位。迟到的心跳靠它 + 终态墓碑挡住，见 project_runtime.accept_status_update。
    status_seq = Column(Integer, default=0, nullable=False, comment="状态水位，只前进")
    attempt = Column(Integer, default=1, nullable=False, comment="第几次尝试，含重试")
    error_code = Column(String(64), nullable=True, comment="失败原因，进死信时要看得见")

    lease_expires_at = Column(DateTime, nullable=True, comment="租约到期时间；续不上即视为死亡")
    started_at = Column(DateTime, nullable=True, comment="进入 running 的时间")
    finished_at = Column(DateTime, nullable=True, comment="进入终态的时间")
    reply_message_id = Column(BigInteger, nullable=True, comment="这次 turn 写回的那条消息")


class RedactionKeyword(Base, TimestampMixin):
    """按部门设置的屏蔽词 —— 阶段 18

    形状照 `ModelWhitelist` / `TokenQuota`：`scope` + `scope_value`，解析时取并集。
    照抄是有意的 —— 后台已经有两个「按部门配」的页面，第三个长得不一样只会让人
    每次都要重新读一遍规则。

    ## 与环境变量 `MASK_KEYWORDS` 的关系是**相加，不是替换**

    环境变量那份继续作为全局基线：已经部署的实例不会因为这张表是空的就突然
    什么都不遮。表里的 `global` 行与它合并，`dept` 行再叠在上面。

    反过来（表存在就忽略环境变量）会造成一个很难查的失败：某天有人建了第一条
    dept 规则，全局那几个词**当场失效**，而没有任何一处会报错。

    ## 群聊里按**发言人**的部门遮，不按读者

    一条消息从 A 部门发出、被 B 部门读到。要防的是「A 的内部术语流到 B」，所以
    生效的是**发言人**部门的词表（并上全局）。

    按读者部门遮是错的：那样 A 的术语在 B 眼里原样呈现，恰恰是要防的那个方向。
    取所有成员部门的并集也想过，没做 —— B 的术语不会出现在 A 写的话里，真出现了
    说明 B 的信息已经泄漏到 A 那儿了，遮这条消息也来不及。多遮只会制造噪音，
    而噪音会让人忽略真正被遮的那些。

    ## ⚠️ `scope_value` 存部门 **ID** 的十进制字符串，不是部门 code

    解析时比的是 `caller.dept_id`（int）。写成 code（`finance`）的行对谁都不生效，
    而列表页看上去完全正常 —— 2026-09-03 线上就是这么漏的。入口侧的防呆在
    `client/src/pages/redaction.vue`：部门只能从下拉里选，不能手打。

    `dept` 行**向下继承**：子部门同时吃自己和所有上级的词
    （`services/org_tree.expand_ancestors`，与技能空间 / 配额同一个方向）。
    """

    __tablename__ = "redaction_keywords"
    __table_args__ = (
        UniqueConstraint("scope", "scope_value", "keyword", name="uq_redaction_keyword"),
        Index("ix_redaction_scope", "scope", "scope_value", "status"),
        {"mysql_collate": "utf8mb4_general_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True, comment="自增ID")
    scope = Column(String(16), nullable=False, comment="global / dept")
    scope_value = Column(
        String(64), nullable=False, default="", comment="部门ID；global 为空串"
    )
    keyword = Column(String(128), nullable=False, comment="要遮的词；匹配大小写不敏感")
    status = Column(Integer, default=1, nullable=False, comment="1启用 0停用")
    remark = Column(String(255), nullable=True, comment="备注：为什么要遮它")
