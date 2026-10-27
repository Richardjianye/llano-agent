"""脱敏词表取不到时，报错必须说清楚是哪一种失败。

起因（2026-09-11）：一位同事强制更新后启动就是
`agent init failed: redaction policy unavailable`，紧跟一条「连不上网关」。
那一句话当时覆盖了六种原因 —— 网络不通、TLS 被安全软件拦截、登录过期、
系统时钟快了几分钟、响应格式坏掉、本地缓存过期 —— 每一种的下一步都不一样，
而界面上长得完全一样，只能靠猜。

所以这些用例钉的不是"报错好不好看"，是**每一类故障都能从报错里读出该做什么**，
以及**该重试的重试、不该重试的别白等**。
"""

from datetime import datetime, timedelta, timezone
import ssl
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from agent import redaction_policy as redaction_policy_module
from agent.redaction_policy import (RedactionPolicyCache,
                                    RedactionPolicyUnavailable,
                                    resolve_product_redaction_policy)

NOW = datetime(2026, 9, 11, 6, 0, tzinfo=timezone.utc)
PRODUCT = SimpleNamespace(provider_id="llano-gateway", gateway_origin="https://gateway.example")


def _payload(*, expires_in: int = 300) -> dict:
    return {
        "object": "llano.redaction_policy",
        "version": "sha256:v1",
        "keywords": ["LLANO"],
        "placeholder": "[公司]",
        "expires_at": (NOW + timedelta(seconds=expires_in)).isoformat().replace("+00:00", "Z"),
    }


def _resolve(fetch_json, *, cache=None, now=NOW, slept=None, clock=None):
    return resolve_product_redaction_policy(
        provider="llano-gateway",
        base_url="https://gateway.example/v1",
        api_key="device-secret",
        product_policy=PRODUCT,
        cache=cache or RedactionPolicyCache(),
        fetch_json=fetch_json,
        now=now,
        sleep=(
            slept
            if callable(slept)
            else slept.append
            if slept is not None
            else (lambda _seconds: None)
        ),
        clock=clock,
    )


def _raises(exc):
    def fetch_json(_url, _headers):
        raise exc

    return fetch_json


class TestFailuresAreDistinguishable:
    def test_network_failure_names_the_outbound_firewall(self):
        # 这台机器上最可能的原因：更新换了 sidecar 二进制，出站防火墙把它当新程序拦了
        with pytest.raises(RedactionPolicyUnavailable) as raised:
            _resolve(_raises(URLError("Connection refused")))

        assert raised.value.reason == "network"
        assert "网络连接失败" in str(raised.value)
        assert "Connection refused" in str(raised.value)
        assert "防火墙" in str(raised.value)

    def test_tls_interception_is_not_reported_as_a_dead_network(self):
        # SSLError 是 OSError 的子类，只看类型会把它归进"网络不通"，
        # 而这两条的排查方向完全相反
        with pytest.raises(RedactionPolicyUnavailable) as raised:
            _resolve(_raises(URLError(ssl.SSLCertVerificationError("unable to get local issuer certificate"))))

        assert raised.value.reason == "tls"
        assert "证书验证失败" in str(raised.value)
        assert "unable to get local issuer certificate" in str(raised.value)

    def test_bare_ssl_error_is_also_classified_as_tls(self):
        with pytest.raises(RedactionPolicyUnavailable) as raised:
            _resolve(_raises(ssl.SSLError("handshake failure")))

        assert raised.value.reason == "tls"

    def test_expired_login_says_log_in_again(self):
        error = HTTPError("https://gateway.example/v1/redaction-policy", 401, "Unauthorized", {}, None)

        with pytest.raises(RedactionPolicyUnavailable) as raised:
            _resolve(_raises(error))

        assert raised.value.reason == "unauthorized"
        assert "重新登录" in str(raised.value)

    def test_a_fast_clock_is_named_as_a_clock_problem(self):
        # 词表 TTL 是 300 秒。到手即过期几乎只有一个解释：本机时间比网关快。
        # 以前这一支被压成 "redaction policy unavailable"，把人引向网关。
        with pytest.raises(RedactionPolicyUnavailable) as raised:
            _resolve(lambda _url, _headers: _payload(expires_in=-600))

        assert raised.value.reason == "clock-skew"
        assert "系统时间" in str(raised.value)
        assert "10 分 0 秒" in str(raised.value)

    def test_malformed_response_says_so(self):
        with pytest.raises(RedactionPolicyUnavailable) as raised:
            _resolve(lambda _url, _headers: {"object": "something.else"})

        assert raised.value.reason == "payload"
        assert "无法解析" in str(raised.value)

    def test_missing_credential_and_off_gateway_keep_their_own_reasons(self):
        with pytest.raises(RedactionPolicyUnavailable) as no_key:
            resolve_product_redaction_policy(
                provider="llano-gateway",
                base_url="https://gateway.example/v1",
                api_key="",
                product_policy=PRODUCT,
                cache=RedactionPolicyCache(),
                fetch_json=lambda *_a, **_k: _payload(),
                now=NOW,
            )

        assert no_key.value.reason == "no-credential"

        with pytest.raises(RedactionPolicyUnavailable) as off_gateway:
            resolve_product_redaction_policy(
                provider="openrouter",
                base_url="https://openrouter.ai/api/v1",
                api_key="k",
                product_policy=PRODUCT,
                cache=RedactionPolicyCache(),
                fetch_json=lambda *_a, **_k: _payload(),
                now=NOW,
            )

        assert off_gateway.value.reason == "provider-mismatch"


class TestRetry:
    def test_default_retry_budget_is_bounded_below_old_single_timeout(
        self, monkeypatch
    ):
        """Three retries must not turn the old 15s timeout into a 47s freeze."""
        timeouts: list[float] = []
        slept: list[float] = []

        def timeout(_request, *, timeout):
            timeouts.append(timeout)
            raise URLError("timed out")

        monkeypatch.setattr(redaction_policy_module, "urlopen", timeout)

        with pytest.raises(RedactionPolicyUnavailable) as raised:
            _resolve(redaction_policy_module._fetch_json, slept=slept)

        assert raised.value.reason == "timeout"
        assert timeouts == [4.0, 4.0, 4.0]
        assert slept == [0.5, 1.5]
        assert sum(timeouts) + sum(slept) == 14.0

    def test_a_blip_no_longer_means_the_app_will_not_start(self):
        attempts = {"n": 0}

        def fetch_json(_url, _headers):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise URLError("temporary failure in name resolution")
            return _payload()

        slept: list[float] = []
        policy = _resolve(fetch_json, slept=slept)

        assert policy.version == "sha256:v1"
        assert attempts["n"] == 3
        # 退避真的等过，而且总时长限制在两秒上下 —— 这是启动路径
        assert slept == [0.5, 1.5]
        assert sum(slept) <= 2.5

    def test_a_rejected_login_is_not_retried(self):
        attempts = {"n": 0}

        def fetch_json(_url, _headers):
            attempts["n"] += 1
            raise HTTPError("https://gateway.example/v1/redaction-policy", 401, "Unauthorized", {}, None)

        slept: list[float] = []

        with pytest.raises(RedactionPolicyUnavailable):
            _resolve(fetch_json, slept=slept)

        # 重试一百次也是同一个 401，只会把启动卡得更久
        assert attempts["n"] == 1
        assert slept == []

    def test_a_malformed_response_is_not_retried_either(self):
        attempts = {"n": 0}

        def fetch_json(_url, _headers):
            attempts["n"] += 1
            return {"object": "nope"}

        with pytest.raises(RedactionPolicyUnavailable):
            _resolve(fetch_json)

        assert attempts["n"] == 1

    def test_gateway_5xx_is_retried_because_a_container_swap_is_transient(self):
        attempts = {"n": 0}

        def fetch_json(_url, _headers):
            attempts["n"] += 1
            raise HTTPError("https://gateway.example/v1/redaction-policy", 503, "Bad Gateway", {}, None)

        with pytest.raises(RedactionPolicyUnavailable) as raised:
            _resolve(fetch_json)

        assert attempts["n"] == 3
        assert raised.value.reason == "server"

    def test_a_valid_cached_policy_still_wins_over_a_failed_refresh(self):
        cache = RedactionPolicyCache(refresh_skew_seconds=60)
        calls = {"n": 0}

        def fetch_json(_url, _headers):
            calls["n"] += 1
            if calls["n"] == 1:
                return _payload(expires_in=300)
            raise URLError("offline")

        first = _resolve(fetch_json, cache=cache)
        # 刷新窗口内再问一次：刷新失败，但手里那份还没过期 —— 不该让人停工
        again = _resolve(fetch_json, cache=cache, now=NOW + timedelta(seconds=250))

        assert again == first

    def test_idle_past_ttl_retries_refresh_instead_of_replaying_expired_error(self):
        """Laptop sleep can cross the five-minute TTL between requests.

        The first request after wake must fetch a new policy, and a single
        transport blip during that refresh must use the bounded retry path.
        The expired cached policy is never returned.
        """
        cache = RedactionPolicyCache(refresh_skew_seconds=60)
        calls = {"n": 0}

        def initial(_url, _headers):
            return _payload(expires_in=300)

        stale = _resolve(initial, cache=cache)

        def after_wake(_url, _headers):
            calls["n"] += 1
            if calls["n"] == 1:
                raise URLError("network still waking")
            return _payload(expires_in=900)

        slept: list[float] = []
        fresh = _resolve(
            after_wake,
            cache=cache,
            now=NOW + timedelta(seconds=301),
            slept=slept,
        )

        assert fresh != stale
        assert fresh.expires_at == NOW + timedelta(seconds=900)
        assert calls["n"] == 2
        assert slept == [0.5]

    def test_retry_crossing_expiry_never_returns_now_expired_cache(self):
        """A valid-at-entry cache may expire while refresh retries are sleeping."""
        cache = RedactionPolicyCache(
            refresh_skew_seconds=60,
            attempts=2,
            backoff_seconds=(2.0,),
        )
        _resolve(lambda _url, _headers: _payload(expires_in=2), cache=cache)

        current = {"value": NOW + timedelta(seconds=1)}

        def clock():
            return current["value"]

        def advance(seconds):
            current["value"] += timedelta(seconds=seconds)

        with pytest.raises(RedactionPolicyUnavailable) as raised:
            _resolve(
                _raises(URLError("offline")),
                cache=cache,
                now=None,
                clock=clock,
                slept=advance,
            )

        assert raised.value.reason == "cache-expired"
        assert current["value"] > NOW + timedelta(seconds=2)

    def test_lock_wait_crossing_expiry_rechecks_time_before_cache_hit(self):
        """Queueing behind another refresh must not make stale time authoritative."""
        cache = RedactionPolicyCache(refresh_skew_seconds=30)
        _resolve(lambda _url, _headers: _payload(expires_in=40), cache=cache)

        current = {"value": NOW}
        calls = {"n": 0}

        def clock():
            return current["value"]

        class _LockWaitCrossesExpiry:
            def __enter__(self):
                # Model a resolver queued behind the process-wide refresh lock
                # long enough for the previously valid policy to expire.
                current["value"] += timedelta(seconds=60)

            def __exit__(self, _exc_type, _exc, _tb):
                return False

        cache._lock = _LockWaitCrossesExpiry()

        def offline(_url, _headers):
            calls["n"] += 1
            raise URLError("offline")

        with pytest.raises(RedactionPolicyUnavailable) as raised:
            _resolve(
                offline,
                cache=cache,
                now=None,
                clock=clock,
                slept=[],
            )

        assert calls["n"] == 3
        assert raised.value.reason == "cache-expired"

    def test_policy_expired_during_retries_is_rejected_at_receipt(self):
        """The last response is checked against its receipt time, not call start."""
        cache = RedactionPolicyCache(
            attempts=3,
            backoff_seconds=(2.0, 2.0),
        )
        current = {"value": NOW}
        calls = {"n": 0}

        def clock():
            return current["value"]

        def advance(seconds):
            current["value"] += timedelta(seconds=seconds)

        def fetch_json(_url, _headers):
            calls["n"] += 1
            if calls["n"] < 3:
                raise URLError("offline")
            return _payload(expires_in=3)

        with pytest.raises(RedactionPolicyUnavailable) as raised:
            _resolve(
                fetch_json,
                cache=cache,
                now=None,
                clock=clock,
                slept=advance,
            )

        assert calls["n"] == 3
        assert current["value"] == NOW + timedelta(seconds=4)
        assert raised.value.reason == "clock-skew"


class TestIdleSessions:
    """2026-09-24：同一会话闲置超过 5 分钟后再发消息，报「脱敏词表已过期，且刷新失败：
    连不上网关：_ssl.c:999: The handshake operation timed out」，多人反馈。"""

    def test_a_handshake_timeout_is_not_blamed_on_the_firewall(self):
        exc = URLError(TimeoutError("_ssl.c:999: The handshake operation timed out"))

        with pytest.raises(RedactionPolicyUnavailable) as raised:
            _resolve(_raises(exc))

        assert raised.value.reason == "timeout"
        assert "超时" in str(raised.value)
        assert "重新发送" in str(raised.value)
        assert "防火墙" not in str(raised.value)

    def test_a_bare_read_timeout_is_also_a_timeout(self):
        with pytest.raises(RedactionPolicyUnavailable) as raised:
            _resolve(_raises(TimeoutError("The read operation timed out")))

        assert raised.value.reason == "timeout"

    def test_mid_session_refresh_waits_longer_than_a_cold_start(self, monkeypatch):
        """手里有一份刚过期的词表 = 同事在等消息发出去：超时放宽到 4/8/12 秒。"""
        cache = RedactionPolicyCache()
        _resolve(lambda _u, _h: _payload(), cache=cache, now=NOW)  # 先拿到一份
        timeouts: list[float] = []

        def timeout(_request, *, timeout):
            timeouts.append(timeout)
            raise URLError(TimeoutError("_ssl.c:999: The handshake operation timed out"))

        monkeypatch.setattr(redaction_policy_module, "urlopen", timeout)
        with pytest.raises(RedactionPolicyUnavailable) as raised:
            _resolve(redaction_policy_module._fetch_json, cache=cache, now=NOW + timedelta(minutes=30))

        assert raised.value.reason == "cache-expired"
        assert timeouts == [4.0, 8.0, 12.0]

    def test_keep_warm_renews_a_policy_before_it_expires(self):
        cache = RedactionPolicyCache()
        cache._keep_warm["k"] = ("https://gateway.example/v1/redaction-policy", "device-secret",
                                 redaction_policy_module.time.monotonic())
        cache._entries["k"] = redaction_policy_module._parse_policy(_payload(expires_in=60))
        renewed = _payload(expires_in=300)

        count = cache.refresh_due(clock=lambda: NOW, fetch_json=lambda _u, _h: renewed)

        assert count == 1
        assert cache._entries["k"].expires_at == NOW + timedelta(seconds=300)

    def test_keep_warm_leaves_a_fresh_policy_alone_and_swallows_failures(self):
        cache = RedactionPolicyCache()
        cache._keep_warm["fresh"] = ("u1", "k1", redaction_policy_module.time.monotonic())
        cache._keep_warm["due"] = ("u2", "k2", redaction_policy_module.time.monotonic())
        cache._entries["fresh"] = redaction_policy_module._parse_policy(_payload(expires_in=280))
        cache._entries["due"] = redaction_policy_module._parse_policy(_payload(expires_in=10))
        calls: list[str] = []

        def fetch(url, _headers):
            calls.append(url)
            raise URLError(TimeoutError("timed out"))

        assert cache.refresh_due(clock=lambda: NOW, fetch_json=fetch) == 0
        assert calls == ["u2"], "没到期的不碰；到期的试一次，失败不抛"
        assert cache._entries["due"].expires_at == NOW + timedelta(seconds=10)

    def test_keep_warm_drops_credentials_nobody_has_used_for_hours(self):
        cache = RedactionPolicyCache()
        stale_use = redaction_policy_module.time.monotonic() - redaction_policy_module._KEEP_WARM_IDLE_SECONDS - 1
        cache._keep_warm["old"] = ("u", "k", stale_use)

        cache.refresh_due(clock=lambda: NOW, fetch_json=lambda *_a: pytest.fail("不该再去拉"))

        assert "old" not in cache._keep_warm

    def test_injected_fetchers_never_start_the_background_thread(self):
        cache = RedactionPolicyCache()
        _resolve(lambda _u, _h: _payload(), cache=cache)

        assert cache._keep_warm == {}
        assert cache._keep_warm_thread is None
