"""Policy for branded desktop distributions, sourced from managed config.

Stock Hermes remains unrestricted. A branded desktop build opts in by
launching its backend with a generated ``HERMES_MANAGED_DIR`` containing
``product.enabled: true``.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any

from hermes_cli.managed_scope import (
    ManagedConfigState,
    load_managed_config,
    load_managed_config_status,
)


@dataclass(frozen=True)
class ProductPolicy:
    brand_id: str
    environment: str
    provider_id: str
    gateway_origin: str
    update_manifest_url: str
    # Empty means unrestricted; nonempty lists restrict registered connectors.
    allowed_messaging_platforms: frozenset[str]
    disabled_capabilities: frozenset[str]
    # 自有 skill hub 的地址。留空 = 还没上线，那时品牌构建一个远程技能源都没有
    # （见 THIRD_PARTY_SKILL_SOURCES）。字段先留着，hub 建好当天客户端就有落点。
    skill_hub_url: str = ""
    # 全新安装时的界面语言。留空 = 不干预，用 DEFAULT_CONFIG 里的 "en"。
    # 见 apply_product_language_default 说明为什么这件事不能靠 managed 覆盖层做。
    default_language: str = ""
    # 仅公司网关托管生图；内置外部供应商仍由原能力开关封锁。
    managed_image_generation: bool = False
    sync_skill_usage: bool = False


class ProductPolicyState(str, Enum):
    STOCK = "stock"
    ACTIVE = "active"
    INVALID = "invalid"


@dataclass(frozen=True)
class ProductPolicyStatus:
    state: ProductPolicyState
    policy: ProductPolicy | None = None
    reason: str = ""


CAPABILITY_UNAVAILABLE = "capability_unavailable"

# 自有更新闭环（阶段 5）之后，官方 git / GitHub ZIP / Nous 更新通道必须不可达。
# 名字单独提成常量是因为它在三处被引用（CLI 闸门、slash 命令表、仪表盘路由），
# 而拼错一个字符串的后果是那一处静默放行。
SELF_UPDATE = "self_update"

# 技能来源。上游的 create_source_router 一次装配 9 个来源，其中 7 个直连公网第三方
# （github / clawhub / skills-sh / well-known / url / lobehub / browse-sh），
# 阶段 5 只给「Nous 中心索引」那一条加了闸门。
#
# 这个能力位关掉之后走的是**白名单**而不是逐个封：月度同步给上游加进来第十个来源
# 时，默认是关的，而不是默认敞开等人发现。
THIRD_PARTY_SKILL_SOURCES = "third_party_skill_sources"

# 窗口枚举（read_window_below）。品牌构建里关掉，两个理由叠在一起：
#
# 1. **隐私**：它返回的是「用户此刻在后面哪个窗口里」的应用名与**窗口标题**
#    （只有元数据，不含像素、不含窗口内容 —— 别把它说成读取内容）。但标题本身
#    可能是文档名、邮件主题、浏览器标签页。而这个产品是全量审计的：模型读到的
#    东西会进对话、进而进审计库，可那些标题从来不是员工主动提交的。
# 2. **打包**：底层的 `get-windows` 是 node-gyp 从源码编译的原生模块，
#    Windows 绑定要 MSVC —— 它是 macOS 上交叉构建 Windows 包唯一的墙
#    （2026-08-25 实测，见 TODO 阶段 6）。
#
# 关能力位**不等于**包里就没有它了：npm 依赖同时从 apps/desktop/package.json
# 拿掉了。两处要一起看 —— 只关能力位，MSVC 那道墙还在；只删依赖，模型还会去调
# 一个永远只回错误的工具。
WINDOW_ENUMERATION = "window_enumeration"


class ProductCapabilityUnavailable(RuntimeError):
    """A branded product policy blocks this capability."""

    reason = CAPABILITY_UNAVAILABLE

    def __init__(self, capability: str):
        self.capability = capability
        super().__init__(self.reason)


_CONFIG_CAPABILITIES: tuple[tuple[str, str], ...] = (
    ("providers", "custom_model_providers"),
    ("custom_providers", "custom_model_providers"),
    ("model.provider", "provider_management"),
    ("model.base_url", "provider_management"),
    ("model.api_mode", "provider_management"),
    ("model.key_env", "provider_management"),
    ("model.api_key", "provider_management"),
    ("fallback_model", "provider_management"),
    ("moa", "moa"),
    ("image_gen", "builtin_image_generation"),
    ("pet.generation", "pet_ai_generation"),
    ("pet.generation_provider", "pet_ai_generation"),
    ("pet.image_provider", "pet_ai_generation"),
    ("nous", "nous_services"),
)

_PROVIDER_ENV_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ARCEEAI_API_KEY",
        "DASHSCOPE_API_KEY",
        "DEEPSEEK_API_KEY",
        "FIREWORKS_API_KEY",
        "GEMINI_API_KEY",
        "GLM_API_KEY",
        "GMI_API_KEY",
        "GOOGLE_API_KEY",
        "HF_TOKEN",
        "KIMI_API_KEY",
        "MINIMAX_API_KEY",
        "NVIDIA_API_KEY",
        "OPENAI_API_KEY",
        "OPENCODE_API_KEY",
        "OPENROUTER_API_KEY",
        "STEPFUN_API_KEY",
        "XAI_API_KEY",
        "ZAI_API_KEY",
    }
)


def load_product_policy_status() -> ProductPolicyStatus:
    """Distinguish intentionally-stock installs from broken managed policy."""
    managed = load_managed_config_status()
    if managed.state is ManagedConfigState.ABSENT:
        return ProductPolicyStatus(ProductPolicyState.STOCK)
    if managed.state is ManagedConfigState.INVALID:
        return ProductPolicyStatus(
            ProductPolicyState.INVALID,
            reason=managed.reason,
        )

    raw = managed.config.get("product")
    if raw is None:
        return ProductPolicyStatus(
            ProductPolicyState.INVALID,
            reason="declared managed config is missing product policy",
        )
    if not isinstance(raw, dict):
        return ProductPolicyStatus(
            ProductPolicyState.INVALID,
            reason="managed product policy is not a mapping",
        )
    if raw.get("enabled") is not True:
        # Missing/false is an explicit stock declaration. Non-boolean truthy
        # values are configuration errors rather than an implicit opt-in.
        if raw.get("enabled") is False:
            return ProductPolicyStatus(ProductPolicyState.STOCK)
        return ProductPolicyStatus(
            ProductPolicyState.INVALID,
            reason="managed product.enabled must be boolean",
        )

    brand_id = str(raw.get("brand_id") or "").strip()
    provider_id = str(raw.get("provider_id") or "").strip()
    if not brand_id or not provider_id:
        return ProductPolicyStatus(
            ProductPolicyState.INVALID,
            reason="enabled managed product is missing brand_id/provider_id",
        )

    allowed = raw.get("allowed_messaging_platforms")
    if allowed is None:
        allowed = []
    elif not isinstance(allowed, list):
        return ProductPolicyStatus(
            ProductPolicyState.INVALID,
            reason="managed product.allowed_messaging_platforms must be a list",
        )
    disabled = raw.get("disabled_capabilities")
    if not isinstance(disabled, list):
        disabled = []

    policy = ProductPolicy(
        brand_id=brand_id,
        environment=str(raw.get("environment") or "").strip(),
        provider_id=provider_id,
        gateway_origin=str(raw.get("gateway_origin") or "").strip(),
        update_manifest_url=str(raw.get("update_manifest_url") or "").strip(),
        allowed_messaging_platforms=frozenset(
            str(value).strip().lower() for value in allowed if str(value).strip()
        ),
        disabled_capabilities=frozenset(
            str(value).strip().lower() for value in disabled if str(value).strip()
        ),
        skill_hub_url=str(raw.get("skill_hub_url") or "").strip(),
        default_language=str(raw.get("default_language") or "").strip(),
        managed_image_generation=raw.get("managed_image_generation") is True,
        sync_skill_usage=raw.get("sync_skill_usage") is True,
    )
    return ProductPolicyStatus(ProductPolicyState.ACTIVE, policy=policy)


def load_product_policy() -> ProductPolicy | None:
    """Return active policy; legacy non-sensitive callers see invalid as none."""
    status = load_product_policy_status()
    return status.policy if status.state is ProductPolicyState.ACTIVE else None


def product_skill_hub_url() -> str:
    """自有 skill hub 的地址，未配置时为空串。"""
    policy = load_product_policy()
    return policy.skill_hub_url if policy else ""


def _capability_allowed_for_status(
    status: ProductPolicyStatus,
    capability: str,
) -> bool:
    if status.state is ProductPolicyState.STOCK:
        return True
    if status.state is ProductPolicyState.INVALID:
        # A declared managed install with unreadable policy is not stock.
        # Callers only ask this helper about governed/sensitive surfaces, so
        # fail closed until IT repairs the configuration.
        return False
    policy = status.policy
    return bool(policy) and capability not in policy.disabled_capabilities


def capability_allowed(capability: str) -> bool:
    """Return whether a governed capability may execute.

    Only an explicit STOCK state opens every capability.  INVALID must remain
    closed or a truncated/temporarily unreadable managed YAML would silently
    re-enable provider escape, Nous, window enumeration, and third-party skill
    sources.
    """
    normalized = capability.strip().lower()
    return _capability_allowed_for_status(
        load_product_policy_status(),
        normalized,
    )


def require_capability(capability: str) -> None:
    """Reject a blocked capability with a stable application reason."""
    normalized = capability.strip().lower()
    if not capability_allowed(normalized):
        raise ProductCapabilityUnavailable(normalized)


def _path_capability(path: str) -> str | None:
    normalized = path.strip().strip(".")
    if normalized.lower().startswith("env."):
        key = normalized[4:].strip().upper()
        if key.startswith("NOUS_"):
            return "nous_services"
        if key in _PROVIDER_ENV_KEYS:
            return "provider_management"
        return None

    normalized = normalized.lower()
    for prefix, capability in _CONFIG_CAPABILITIES:
        if normalized == prefix or normalized.startswith(f"{prefix}."):
            return capability
    return None


def config_write_allowed(path: str, value: object = None) -> bool:
    """Allow unrelated preferences while rejecting policy-sensitive fields."""
    del value
    capability = _path_capability(path)
    return capability is None or capability_allowed(capability)


def sanitize_product_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return a safe effective view without mutating the user's source config."""
    sanitized = deepcopy(config)
    status = load_product_policy_status()
    if status.state is ProductPolicyState.STOCK:
        return sanitized
    policy = status.policy

    if status.state is ProductPolicyState.INVALID:
        # Without a valid provider pin there is no safe arbitrary provider to
        # substitute. Remove every governed execution setting so downstream
        # resolution fails visibly instead of escaping through user config.
        sanitized["providers"] = {}
        sanitized["custom_providers"] = {}
        sanitized["model"] = {}
        sanitized["fallback_model"] = {}
        sanitized["moa"] = {}
        sanitized["image_gen"] = {}
        sanitized["nous"] = {}
        pet = sanitized.get("pet")
        if isinstance(pet, dict):
            for key in ("generation", "generation_provider", "image_provider"):
                pet.pop(key, None)
        return sanitized

    assert policy is not None

    if not _capability_allowed_for_status(status, "custom_model_providers"):
        sanitized["providers"] = {}
        sanitized["custom_providers"] = {}

    if not _capability_allowed_for_status(status, "provider_management"):
        model = sanitized.get("model")
        if isinstance(model, dict):
            safe_model = {"provider": policy.provider_id}
            model_name = model.get("default", model.get("name", model.get("model")))
            if model_name:
                safe_model["default"] = model_name
            sanitized["model"] = safe_model
        sanitized["fallback_model"] = {}

    if not _capability_allowed_for_status(status, "moa"):
        sanitized["moa"] = {}

    if not _capability_allowed_for_status(status, "builtin_image_generation"):
        sanitized["image_gen"] = {}

    if not _capability_allowed_for_status(status, "pet_ai_generation"):
        pet = sanitized.get("pet")
        if isinstance(pet, dict):
            for key in ("generation", "generation_provider", "image_provider"):
                pet.pop(key, None)

    if not _capability_allowed_for_status(status, "nous_services"):
        sanitized["nous"] = {}

    return sanitized


def _display_language_is_pinned() -> bool:
    """用户或管理员是不是已经明确指定过界面语言。

    必须读**没合并过默认值**的用户配置：load_config() 从 DEFAULT_CONFIG 起步，
    里面 display.language 恒为 "en"，合并之后这个键永远存在，「用户设过」和
    「用户没设过」在合并结果里长得一模一样。
    """
    from hermes_cli.config import read_user_config_raw

    # 管理员在 managed 配置里钉过就当已指定 —— 那一层是盖在用户配置**之上**的
    # （见 managed_scope.apply_managed_overlay），品牌默认值不该再压过它。
    managed_display = load_managed_config().get("display")
    if isinstance(managed_display, dict) and str(managed_display.get("language") or "").strip():
        return True

    try:
        raw = read_user_config_raw()
    except Exception:  # noqa: BLE001 — 读不到就别自作主张改用户看到的语言
        return True

    display = raw.get("display")
    if not isinstance(display, dict):
        return False

    return bool(str(display.get("language") or "").strip())


def apply_product_language_default(config: dict[str, Any]) -> dict[str, Any]:
    """品牌构建里，用户没选过语言时把界面默认语言填成产品自己的那个。

    **为什么不直接改 DEFAULT_CONFIG**：那是上游文件，改了连普通 Hermes 也一起变。

    **为什么不放进 managed 配置**：managed 覆盖层是 leaf-level 盖在用户配置
    *之上*的，往那儿钉 display.language 会把用户自己选的语言也一起压掉 ——
    等于把语言开关做成了摆设。

    **为什么只在 GET 侧调、不放进 sanitize_product_config**：PUT /api/config 也
    走 sanitize。用户第一次把语言切成英文时，磁盘上的 raw 配置还没有这个键，
    在那条路径上填默认值会把刚提交的 "en" 反向覆盖回 "zh"，表现为「切了不生效，
    再切一次才行」。这是个视图默认值，不是写入值。

    落地效果：全新安装第一眼就是产品语言（员工不必先登录、再去设置里找开关）；
    用户一旦自己选过，raw 配置里就有了这个键，这里再不插手。
    """
    status = load_product_policy_status()
    policy = status.policy
    if status.state is not ProductPolicyState.ACTIVE or not policy:
        return config
    if not policy.default_language:
        return config

    if _display_language_is_pinned():
        return config

    patched = deepcopy(config)
    display = patched.get("display")
    patched["display"] = (
        {**display, "language": policy.default_language}
        if isinstance(display, dict)
        else {"language": policy.default_language}
    )
    return patched


def messaging_platform_allowed(platform_id: str) -> bool:
    status = load_product_policy_status()
    if status.state is ProductPolicyState.STOCK:
        return True
    if status.state is ProductPolicyState.INVALID or status.policy is None:
        return False
    # An absent/empty allowlist leaves the registered connector catalog open.
    # Explicit lists continue to restrict discovery, writes, and execution.
    allowed = status.policy.allowed_messaging_platforms
    return not allowed or platform_id.strip().lower() in allowed


def model_switch_allowed(provider_id: str | None = None) -> bool:
    """Allow model changes without allowing a branded build to escape its gateway.

    ``None`` means the caller is changing only the model on the current managed
    provider.  An explicit provider must match the managed provider id.
    """
    status = load_product_policy_status()
    if status.state is ProductPolicyState.STOCK:
        return True
    if status.state is ProductPolicyState.INVALID or status.policy is None:
        return False
    if provider_id is None:
        return True
    return provider_id.strip().lower() == status.policy.provider_id.lower()


def apply_messaging_policy(config) -> None:
    """Disable blocked adapters in-place after every configuration source."""
    if load_product_policy_status().state is ProductPolicyState.STOCK:
        return
    for platform, platform_config in config.platforms.items():
        if not messaging_platform_allowed(str(platform.value)):
            platform_config.enabled = False
