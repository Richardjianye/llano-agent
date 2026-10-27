"""Authenticated redaction-policy cache for branded model requests."""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


class RedactionPolicyUnavailable(RuntimeError):
    """A branded request cannot safely proceed without a current policy.

    ``reason`` says **which** of the failure modes this was. The message that
    reaches the user is built from it, because the single sentence this class
    used to carry ("redaction policy unavailable") covered six different
    situations with six different fixes — network down, TLS intercepted by
    corporate security software, the login having expired, a clock that is
    minutes fast, a malformed response, and a stale cache. Support then had to
    guess. 2026-09-11 cost an afternoon of guessing on exactly that.
    """

    def __init__(self, message: str, *, reason: str = "unavailable"):
        super().__init__(message)
        self.reason = reason


#: 传输层与 5xx 值得重试（网络抖动、网关正在换容器）；401/403 与响应格式错
#: 重试一百次也是同一个结果，重试只是把启动卡得更久。
_RETRYABLE_REASONS = frozenset({"network", "timeout", "tls", "server"})


def _is_timeout(exc: BaseException) -> bool:
    """握手 / 读取超时。urllib 会把它包进 URLError.reason，也可能直接抛 TimeoutError。"""
    inner = getattr(exc, "reason", None)
    for candidate in (exc, inner):
        if isinstance(candidate, TimeoutError):
            return True
        if isinstance(candidate, BaseException) and "timed out" in str(candidate):
            return True
    return isinstance(inner, str) and "timed out" in inner


def _describe_failure(exc: BaseException) -> tuple[str, str]:
    """把底层异常翻译成 (reason, 给人看的一句话)。

    这里刻意把**原始异常文本也带上**：分类是为了给出可执行的下一步，
    而原文是唯一能让人看出「到底卡在哪」的东西 —— 上一版把它丢掉了，
    于是所有故障在界面上长得一模一样。
    """
    if isinstance(exc, HTTPError):
        if exc.code in (401, 403):
            return (
                "unauthorized",
                f"登录凭证已失效（HTTP {exc.code}），请退出登录后重新登录。",
            )
        if exc.code >= 500:
            return ("server", f"网关暂时不可用（HTTP {exc.code}）。")
        return ("http", f"网关返回 HTTP {exc.code}。")

    # SSLError 是 OSError 的子类，URLError 会把它包在 .reason 里 —— 两层都要看，
    # 否则企业里最常见的那种故障（安全软件做 HTTPS 拦截）会被归进"网络不通"，
    # 而那两条的排查方向完全不同。
    inner = getattr(exc, "reason", None)
    if isinstance(exc, ssl.SSLError) or isinstance(inner, ssl.SSLError):
        return (
            "tls",
            f"证书验证失败：{inner or exc}。"
            "多半是 VPN／零信任／杀毒软件在做 HTTPS 拦截 —— 它的根证书只装进了钥匙串，"
            "而本组件走的是系统 OpenSSL 证书（/etc/ssl/cert.pem）。",
        )

    # CUSTOM（2026-09-24）：超时单独一类。它和「连不上」的排查方向不同——多数是电脑刚从
    # 休眠唤醒、切换了网络、或代理的上游连接还没恢复，稍后重发就好；把它说成
    # 「出站防火墙要重新放行」会把同事引到完全不相干的地方（09-24 多人反馈）。
    if isinstance(exc, (URLError, OSError)) and _is_timeout(exc):
        return (
            "timeout",
            f"连接网关超时：{getattr(exc, 'reason', None) or exc}。"
            "电脑刚从休眠唤醒、切换网络或代理不稳定时常见，稍等几秒重新发送即可；"
            "一直这样再检查网络与代理。",
        )

    if isinstance(exc, (URLError, OSError)):
        return (
            "network",
            f"网络连接失败：{inner or exc}。"
            "请检查网络连接与代理；如果装了出站防火墙（LuLu／Little Snitch／火绒等），"
            "更新后本应用的后台组件是一个全新的可执行文件，需要重新放行。",
        )

    if isinstance(exc, RedactionPolicyUnavailable):
        return (exc.reason, str(exc))

    return ("payload", f"网关返回的词表无法解析：{exc}。")


@dataclass(frozen=True)
class RedactionPolicy:
    version: str
    keywords: tuple[str, ...]
    placeholder: str
    expires_at: datetime


FetchJson = Callable[[str, dict[str, str]], dict[str, Any]]

# This endpoint returns a tiny JSON document from the same gateway used for
# inference.  A 15-second socket timeout made one wake-from-sleep miss freeze
# agent initialization for 15 seconds; combining it with the three-attempt
# retry added below would have stretched the worst case to ~47 seconds.  Keep
# the full refresh budget bounded: 3 * 4s I/O + 0.5s + 1.5s backoff = 14s.
_FETCH_TIMEOUT_SECONDS = 4.0
_DEFAULT_ATTEMPTS = 3
_DEFAULT_BACKOFF_SECONDS = (0.5, 1.5)

# CUSTOM（2026-09-24）：会话中途、手里已有一份（刚过期的）词表时，同事正在等自己那条
# 消息发出去 —— 这时宁可多等也别报错。电脑刚唤醒时网卡重连、代理重建上游连接常常要
# 5–15 秒，三次 4 秒的握手预算恰好全部落空（09-24 日志：15:18:46 发起，15:19:00 失败，
# 正好 14 秒）。所以这一种情况逐次放宽到 4 / 8 / 12 秒，最坏约 26 秒。冷启动（没有缓存）
# 仍按上面的 14 秒上限，不让启动卡住。
_STALE_CACHE_TIMEOUTS = (4.0, 8.0, 12.0)

# 后台保温：词表到期前这么多秒由后台线程续上，让「闲置一阵再发消息」不必当场连网。
# 超过 _KEEP_WARM_IDLE_SECONDS 没人用过的凭据不再续（下班之后不必整夜打网关）。
_KEEP_WARM_LEAD_SECONDS = 90
_KEEP_WARM_POLL_SECONDS = 30
_KEEP_WARM_IDLE_SECONDS = 8 * 3600


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_policy(payload: object) -> RedactionPolicy:
    if not isinstance(payload, dict) or payload.get("object") != "llano.redaction_policy":
        raise ValueError("invalid redaction policy object")

    version = payload.get("version")
    keywords = payload.get("keywords")
    placeholder = payload.get("placeholder")
    expires_raw = payload.get("expires_at")
    if not isinstance(version, str) or not version:
        raise ValueError("invalid redaction policy version")
    if not isinstance(keywords, list) or any(not isinstance(value, str) or not value for value in keywords):
        raise ValueError("invalid redaction policy keywords")
    if not isinstance(placeholder, str) or not placeholder:
        raise ValueError("invalid redaction policy placeholder")
    if not isinstance(expires_raw, str):
        raise ValueError("invalid redaction policy expiry")

    expires_at = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
    if expires_at.tzinfo is None:
        raise ValueError("redaction policy expiry must include a timezone")

    return RedactionPolicy(
        version=version,
        keywords=tuple(keywords),
        placeholder=placeholder,
        expires_at=expires_at.astimezone(timezone.utc),
    )


def _fetch_json(
    url: str, headers: dict[str, str], timeout: float = _FETCH_TIMEOUT_SECONDS
) -> dict[str, Any]:
    request = Request(url, headers=headers, method="GET")
    with urlopen(  # noqa: S310 - managed HTTPS/loopback URL
        request, timeout=timeout
    ) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("redaction policy response is not an object")
    return payload


class RedactionPolicyCache:
    def __init__(
        self,
        *,
        refresh_skew_seconds: int = 30,
        attempts: int = _DEFAULT_ATTEMPTS,
        backoff_seconds: tuple[float, ...] = _DEFAULT_BACKOFF_SECONDS,
    ):
        self._refresh_skew_seconds = max(0, int(refresh_skew_seconds))
        # 一次网络抖动不该等于"今天开不了工"。只对传输层失败与 5xx 重试；
        # 退避总共至多 2 秒，连同上面的单次 I/O 上限，默认最坏 14 秒。
        self._attempts = max(1, int(attempts))
        self._backoff_seconds = backoff_seconds
        self._entries: dict[str, RedactionPolicy] = {}
        self._lock = threading.Lock()
        # 后台保温登记：cache_key -> (url, api_key, 最近一次被用的单调时钟)。
        # 只登记走默认 _fetch_json 的调用；测试注入的假 fetcher 不会触发后台线程。
        self._keep_warm: dict[str, tuple[str, str, float]] = {}
        self._keep_warm_lock = threading.Lock()
        self._keep_warm_thread: threading.Thread | None = None

    @staticmethod
    def _key(url: str, api_key: str) -> str:
        credential_id = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        return f"{url}|{credential_id}"

    def _fetch_once(
        self,
        *,
        url: str,
        api_key: str,
        fetch_json: FetchJson,
        clock: Callable[[], datetime],
    ) -> RedactionPolicy:
        fetched = _parse_policy(
            fetch_json(
                url,
                {"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
            )
        )

        # Validate at receipt time, not at the start of resolve().  A retry can
        # spend up to fourteen seconds in network I/O/backoff; reusing the
        # entry timestamp here could accept a policy that expired in transit.
        current_time = clock().astimezone(timezone.utc)
        if fetched.expires_at <= current_time:
            # 词表刚拿到手就已经过期。网关签发时给了 5 分钟 TTL，所以这几乎只有
            # 一个解释：**本机时间比网关快**。报"过期"会把人引向网关，报时钟才
            # 指得对方向 —— 这一条以前被归进笼统的 unavailable 里。
            ahead = (current_time - fetched.expires_at).total_seconds()
            raise RedactionPolicyUnavailable(
                f"本机时间比网关下发的词表有效期还晚 {int(ahead) // 60} 分 {int(ahead) % 60} 秒，"
                "词表到手即过期。请把系统时间设为自动同步后重试。",
                reason="clock-skew",
            )

        return fetched

    def resolve(
        self,
        *,
        url: str,
        api_key: str,
        fetch_json: FetchJson = _fetch_json,
        now: datetime | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] | None = None,
    ) -> RedactionPolicy:
        # ``now`` remains a fixed-time compatibility hook for existing tests.
        # Production callers omit it and therefore re-read the real clock after
        # every fetch and before any cached fallback.  Tests that need time to
        # advance deterministically can inject ``clock`` instead.
        read_time = (lambda: now) if now is not None else (clock or _utc_now)
        cache_key = self._key(url, api_key)

        with self._lock:
            # Another resolver can hold this process-wide lock for the whole
            # bounded refresh cycle.  Re-read the clock *after* waiting for
            # that lock: a cache entry that still had room before contention
            # may have crossed its refresh window (or even expired) while this
            # caller was queued.  Using the pre-lock timestamp would return an
            # actually expired policy and break the fail-closed guarantee.
            current_time = read_time().astimezone(timezone.utc)
            cached = self._entries.get(cache_key)
            if fetch_json is _fetch_json and now is None and clock is None:
                self._register_keep_warm(cache_key, url, api_key)
            if cached and (cached.expires_at - current_time).total_seconds() > self._refresh_skew_seconds:
                return cached

            last_exc: BaseException | None = None
            reason = "unavailable"
            detail = ""
            # 手里有一份（快要或已经过期的）词表 = 会话中途，同事在等消息发出去：放宽超时。
            timeouts = _STALE_CACHE_TIMEOUTS if cached else ()

            for attempt in range(self._attempts):
                fetcher = fetch_json
                if fetch_json is _fetch_json and attempt < len(timeouts):
                    fetcher = functools.partial(_fetch_json, timeout=timeouts[attempt])
                try:
                    fetched = self._fetch_once(
                        url=url,
                        api_key=api_key,
                        fetch_json=fetcher,
                        clock=read_time,
                    )
                    self._entries[cache_key] = fetched

                    return fetched
                except Exception as exc:  # noqa: BLE001 — 分类在 _describe_failure 里
                    last_exc, (reason, detail) = exc, _describe_failure(exc)

                    if reason not in _RETRYABLE_REASONS or attempt == self._attempts - 1:
                        break

                    delay = self._backoff_seconds[min(attempt, len(self._backoff_seconds) - 1)]
                    sleep(delay)

            # 手里还有一份没过期的词表时，刷新失败不该让人停工 —— 用旧的继续，
            # 下一次到期前再试。这条在重试之后仍然成立。
            fallback_time = read_time().astimezone(timezone.utc)
            if cached and cached.expires_at > fallback_time:
                return cached

            if cached:
                raise RedactionPolicyUnavailable(
                    f"本地缓存的脱敏词表已过期，且刷新失败：{detail}",
                    reason="cache-expired",
                ) from last_exc

            raise RedactionPolicyUnavailable(
                f"无法获取脱敏词表：{detail}", reason=reason
            ) from last_exc

    # ---- 后台保温（CUSTOM 2026-09-24）----------------------------------------
    #
    # 词表 TTL 只有 5 分钟，而过去只在发消息时按需拉取：闲置超过 5 分钟，下一条消息就必须
    # 当场连网关。恰好赶上网络没恢复（休眠唤醒、代理重连）就被拦下。这里在到期前 90 秒
    # 由后台线程续上。
    #
    # 两条约束：
    # - **网络 I/O 不持有 self._lock**：否则一次慢刷新会把同事正在发的消息卡在锁上；
    #   只在写回时短暂拿锁，并且只在拿到的新词表比现有的更晚到期时才写回。
    # - 失败只记 debug、不抛：保温是锦上添花，真到了发消息时 resolve() 会照常重试并报错。

    def _register_keep_warm(self, cache_key: str, url: str, api_key: str) -> None:
        with self._keep_warm_lock:
            self._keep_warm[cache_key] = (url, api_key, time.monotonic())
            if self._keep_warm_thread is None or not self._keep_warm_thread.is_alive():
                self._keep_warm_thread = threading.Thread(
                    target=self._keep_warm_loop, name="redaction-policy-keep-warm", daemon=True
                )
                self._keep_warm_thread.start()

    def refresh_due(self, *, clock: Callable[[], datetime] = _utc_now,
                    fetch_json: FetchJson = _fetch_json) -> int:
        """把快到期（或已过期）的已登记词表续上一次。返回成功续上的条数。"""
        with self._keep_warm_lock:
            now_mono = time.monotonic()
            for key in [k for k, (_u, _a, used) in self._keep_warm.items()
                        if now_mono - used > _KEEP_WARM_IDLE_SECONDS]:
                del self._keep_warm[key]
            targets = list(self._keep_warm.items())

        refreshed = 0
        for cache_key, (url, api_key, _used) in targets:
            current = self._entries.get(cache_key)
            current_time = clock().astimezone(timezone.utc)
            if current and (current.expires_at - current_time).total_seconds() > _KEEP_WARM_LEAD_SECONDS:
                continue
            try:
                fetched = self._fetch_once(url=url, api_key=api_key, fetch_json=fetch_json, clock=clock)
            except Exception as exc:  # noqa: BLE001 — 保温失败不影响发消息时的重试
                logger.debug("redaction policy keep-warm refresh failed: %s", _describe_failure(exc)[1])
                continue
            with self._lock:
                existing = self._entries.get(cache_key)
                if existing is None or fetched.expires_at > existing.expires_at:
                    self._entries[cache_key] = fetched
                    refreshed += 1
        return refreshed

    def _keep_warm_loop(self) -> None:
        while True:
            time.sleep(_KEEP_WARM_POLL_SECONDS)
            with self._keep_warm_lock:
                if not self._keep_warm:
                    self._keep_warm_thread = None
                    return
            try:
                self.refresh_due()
            except Exception:  # noqa: BLE001 — 后台线程不能因为一次意外就死掉
                logger.debug("redaction policy keep-warm loop error", exc_info=True)


_POLICY_CACHE = RedactionPolicyCache()


def _normalize_endpoint(value: str) -> str:
    return str(value or "").strip().rstrip("/").lower()


def _is_managed_endpoint(provider: str, base_url: str, product_policy) -> bool:
    """Whether this runtime is talking to the branded gateway.

    The obvious check — ``provider == product_policy.provider_id`` — is not
    sufficient, and the reason is upstream behavior we do not control:

    ``resolve_*`` in ``agent/auxiliary_client.py`` decides whether to keep a
    provider's identity alongside an explicit ``base_url`` by calling
    ``hermes_cli.providers.get_provider``, which resolves **built-in providers
    only** (its own docstring points user-config providers at
    ``resolve_provider_full``). ``llano-gateway`` is defined by the managed
    ``providers:`` section, so it is not built in, and the runtime provider
    collapses to ``"custom"`` while the endpoint stays exactly right.

    So the identity is lost but the routing is not. Matching on the endpoint
    recovers what the rename threw away.

    This is deliberately **not** "trust any custom provider": the base URL must
    still be the managed gateway. A personal config cannot point somewhere else
    — managed leaves win per-key — so there is no path here for an arbitrary
    endpoint to be treated as the company gateway and receive the policy fetch.
    """
    normalized = str(provider or "").strip().lower()

    if normalized == str(product_policy.provider_id).strip().lower():
        return True

    if normalized != "custom" and not normalized.startswith("custom:"):
        return False

    actual = _normalize_endpoint(base_url)
    if not actual:
        return False

    origin = _normalize_endpoint(getattr(product_policy, "gateway_origin", ""))
    if not origin:
        return False

    # 托管配置里 base_url 是 <origin>/v1；两种写法都认，但都必须落在同一 origin 上
    return actual == origin or actual.startswith(f"{origin}/")


def resolve_product_redaction_policy(
    *,
    provider: str,
    base_url: str,
    api_key: str,
    product_policy=None,
    cache: RedactionPolicyCache = _POLICY_CACHE,
    fetch_json: FetchJson = _fetch_json,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] | None = None,
) -> RedactionPolicy | None:
    """Resolve policy only for the provider pinned by a branded distribution."""
    if product_policy is None:
        from hermes_cli.product_policy import (
            ProductPolicyState,
            load_product_policy_status,
        )

        product_status = load_product_policy_status()
        if product_status.state is ProductPolicyState.INVALID:
            raise RedactionPolicyUnavailable(
                "公司配置暂时读取不到，为保护数据安全，这条消息没有发出。",
                reason="managed-product-policy-invalid",
            )
        product_policy = product_status.policy
    if product_policy is None:
        return None
    if not _is_managed_endpoint(provider, base_url, product_policy):
        raise RedactionPolicyUnavailable(
            f"当前模型没有通过公司网关（provider={provider or '未知'} base_url={base_url or '空'}），"
            "为保护数据安全，这条消息没有发出。",
            reason="provider-mismatch",
        )
    if not api_key:
        raise RedactionPolicyUnavailable(
            "登录凭证已失效，请重新登录。", reason="no-credential"
        )

    policy_base = base_url.strip().rstrip("/")
    if not policy_base:
        policy_base = f"{str(product_policy.gateway_origin).strip().rstrip('/')}/v1"
    return cache.resolve(
        url=f"{policy_base}/redaction-policy",
        api_key=api_key,
        fetch_json=fetch_json,
        now=now,
        sleep=sleep,
        clock=clock,
    )


def preload_agent_redaction_policy(agent) -> RedactionPolicy | None:
    """Warm the branded policy cache as soon as an agent runtime is ready."""
    return resolve_product_redaction_policy(
        provider=str(getattr(agent, "provider", "") or ""),
        base_url=str(getattr(agent, "base_url", "") or ""),
        api_key=(
            agent.api_key
            if isinstance(getattr(agent, "api_key", None), str)
            else ""
        ),
    )
