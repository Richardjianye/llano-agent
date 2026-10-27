"""Cache-contract tests for ``cached_fetch_api_models()``.

Custom OpenAI-compatible endpoints (named ``custom_providers`` rows, bare
``provider: custom``, and per-endpoint-map entries) previously called
``fetch_api_models()`` directly with no disk cache, so the current custom
endpoint's ``/v1/models`` got a live HTTP round-trip on literally every
``/model`` open (#72762). ``cached_fetch_api_models()`` gives custom
endpoints the same ``provider_models_cache.json`` TTL cache first-class
providers already get via ``cached_provider_model_ids()``.

These pin the cache contract directly (hit / stale / rotation / refresh /
fallback), separate from ``test_model_switch_custom_providers.py``'s
higher-level picker-shape tests.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest


class TestCachedFetchApiModels:
    def _entry(self, models, age_seconds, fp="fp"):
        return {"fp": fp, "at": time.time() - age_seconds, "models": list(models)}

    def test_fresh_entry_served_without_live_fetch(self):
        import hermes_cli.models as mod

        cache = {"custom:https://gw.example.com/v1": self._entry(["m1", "m2"], age_seconds=10)}
        with patch.object(mod, "_load_provider_models_cache", return_value=cache), \
             patch.object(mod, "_custom_endpoint_fingerprint", return_value="fp"), \
             patch.object(mod, "_save_provider_models_cache") as save, \
             patch.object(mod, "fetch_api_models") as live:
            out = mod.cached_fetch_api_models("sk-key", "https://gw.example.com/v1")
        assert out == ["m1", "m2"]
        live.assert_not_called()
        save.assert_not_called()

    def test_cache_key_normalizes_trailing_slash_and_case(self):
        """A saved entry for the lowercased/rstripped URL must be hit even
        when the caller passes a differently-cased URL with a trailing
        slash — config.yaml entries are not guaranteed to be normalized."""
        import hermes_cli.models as mod

        cache = {"custom:https://gw.example.com/v1": self._entry(["m1"], age_seconds=10)}
        with patch.object(mod, "_load_provider_models_cache", return_value=cache), \
             patch.object(mod, "_custom_endpoint_fingerprint", return_value="fp"), \
             patch.object(mod, "fetch_api_models") as live:
            out = mod.cached_fetch_api_models("sk-key", "HTTPS://GW.example.com/v1/")
        assert out == ["m1"]
        live.assert_not_called()

    def test_expired_entry_triggers_live_fetch_and_is_persisted(self):
        import hermes_cli.models as mod

        # Beyond the stale-serve window, so the wrapper must block on a
        # live fetch (within the window it stale-serves + refreshes off
        # thread — covered in TestSalvageFollowups).
        too_old = mod._PROVIDER_MODELS_STALE_SERVE_MAX + 60
        cache = {"custom:https://gw.example.com/v1": self._entry(["old"], age_seconds=too_old)}
        saved = {}
        with patch.object(mod, "_load_provider_models_cache", return_value=cache), \
             patch.object(mod, "_custom_endpoint_fingerprint", return_value="fp"), \
             patch.object(mod, "_save_provider_models_cache", side_effect=saved.update), \
             patch.object(mod, "fetch_api_models", return_value=["fresh-a", "fresh-b"]) as live:
            out = mod.cached_fetch_api_models(
                "sk-key", "https://gw.example.com/v1", ttl_seconds=3600
            )
        assert out == ["fresh-a", "fresh-b"]
        live.assert_called_once()
        assert saved["custom:https://gw.example.com/v1"]["models"] == ["fresh-a", "fresh-b"]
        assert saved["custom:https://gw.example.com/v1"]["fp"] == "fp"

    def test_rotated_api_key_busts_cache_even_when_fresh(self):
        """A same-age entry with a DIFFERENT fingerprint (key rotated, or
        extra_headers edited) must not be served — it reflects the old
        credentials' catalog."""
        import hermes_cli.models as mod

        cache = {"custom:https://gw.example.com/v1": self._entry(["old-key-models"], 10, fp="old-fp")}
        with patch.object(mod, "_load_provider_models_cache", return_value=cache), \
             patch.object(mod, "_custom_endpoint_fingerprint", return_value="new-fp"), \
             patch.object(mod, "_save_provider_models_cache"), \
             patch.object(mod, "fetch_api_models", return_value=["new-key-models"]) as live:
            out = mod.cached_fetch_api_models("sk-new-key", "https://gw.example.com/v1")
        assert out == ["new-key-models"]
        live.assert_called_once()

    def test_force_refresh_bypasses_fresh_cache(self):
        import hermes_cli.models as mod

        cache = {"custom:https://gw.example.com/v1": self._entry(["stale-but-fresh"], age_seconds=5)}
        with patch.object(mod, "_load_provider_models_cache", return_value=cache), \
             patch.object(mod, "_custom_endpoint_fingerprint", return_value="fp"), \
             patch.object(mod, "_save_provider_models_cache"), \
             patch.object(mod, "fetch_api_models", return_value=["forced-live"]) as live:
            out = mod.cached_fetch_api_models(
                "sk-key", "https://gw.example.com/v1", force_refresh=True
            )
        assert out == ["forced-live"]
        live.assert_called_once()

    def test_live_failure_falls_back_to_stale_same_fingerprint_entry(self):
        """Stale data beats no data when the endpoint is flaky (#72762
        proposed-fix: 'same stale-beats-nothing fallback as
        cached_provider_model_ids')."""
        import hermes_cli.models as mod

        cache = {"custom:https://gw.example.com/v1": self._entry(["last-known-good"], age_seconds=99999, fp="fp")}
        with patch.object(mod, "_load_provider_models_cache", return_value=cache), \
             patch.object(mod, "_custom_endpoint_fingerprint", return_value="fp"), \
             patch.object(mod, "_save_provider_models_cache") as save, \
             patch.object(mod, "fetch_api_models", return_value=None):
            out = mod.cached_fetch_api_models("sk-key", "https://gw.example.com/v1")
        assert out == ["last-known-good"]
        save.assert_not_called()  # nothing new to persist

    def test_live_failure_with_no_matching_entry_returns_live_value(self):
        import hermes_cli.models as mod

        with patch.object(mod, "_load_provider_models_cache", return_value={}), \
             patch.object(mod, "_custom_endpoint_fingerprint", return_value="fp"), \
             patch.object(mod, "_save_provider_models_cache") as save, \
             patch.object(mod, "fetch_api_models", return_value=None):
            out = mod.cached_fetch_api_models("sk-key", "https://gw.example.com/v1")
        assert out is None
        save.assert_not_called()


class TestCacheOnly:
    """``cache_only=True`` is the no-network read used by picker opens that
    deliberately skip live probing. It must answer from disk or not at all —
    never a live fetch, never a background revalidation."""

    def _entry(self, models, age_seconds, fp="fp"):
        return {"fp": fp, "at": time.time() - age_seconds, "models": list(models)}

    def _call(self, cache, *, fp="fp", **kwargs):
        import hermes_cli.models as mod

        with patch.object(mod, "_load_provider_models_cache", return_value=cache), \
             patch.object(mod, "_custom_endpoint_fingerprint", return_value=fp), \
             patch.object(mod, "_save_provider_models_cache") as save, \
             patch.object(mod, "_spawn_swr_refresh") as swr, \
             patch.object(mod, "fetch_api_models") as live:
            out = mod.cached_fetch_api_models(
                "sk-key", "https://gw.example.com/v1", cache_only=True, **kwargs
            )
        live.assert_not_called()
        swr.assert_not_called()
        save.assert_not_called()
        return out

    def test_fresh_entry_is_served(self):
        cache = {"custom:https://gw.example.com/v1": self._entry(["m1", "m2"], 10)}
        assert self._call(cache) == ["m1", "m2"]

    def test_entry_past_ttl_is_still_served_within_the_stale_window(self):
        """The TTL governs when to *revalidate*, and cache_only cannot. Inside
        the stale-serve bound the entry is still the best answer available —
        collapsing to the config subset an hour in would reintroduce the bug."""
        import hermes_cli.models as mod

        age = mod._PROVIDER_MODELS_CACHE_TTL + 60
        cache = {"custom:https://gw.example.com/v1": self._entry(["m1", "m2"], age)}
        assert self._call(cache) == ["m1", "m2"]

    def test_entry_beyond_the_stale_window_is_a_miss(self):
        import hermes_cli.models as mod

        age = mod._PROVIDER_MODELS_STALE_SERVE_MAX + 60
        cache = {"custom:https://gw.example.com/v1": self._entry(["ancient"], age)}
        assert self._call(cache) is None

    def test_empty_cache_is_a_miss(self):
        assert self._call({}) is None

    def test_rotated_credentials_are_a_miss(self):
        cache = {"custom:https://gw.example.com/v1": self._entry(["old"], 10, fp="old-fp")}
        assert self._call(cache, fp="new-fp") is None

    def test_force_refresh_is_a_miss_rather_than_a_live_fetch(self):
        """cache_only outranks force_refresh: the caller has said no network,
        so an un-revalidatable entry is withheld instead of fetched."""
        cache = {"custom:https://gw.example.com/v1": self._entry(["m1"], 10)}
        assert self._call(cache, force_refresh=True) is None

    def test_missing_base_url_is_a_miss_rather_than_a_live_fetch(self):
        import hermes_cli.models as mod

        with patch.object(mod, "fetch_api_models") as live:
            out = mod.cached_fetch_api_models("sk-key", "", cache_only=True)
        assert out is None
        live.assert_not_called()

    def test_empty_live_result_is_not_persisted(self):
        """An empty list from a transient error must never pin an empty
        cache entry over real data on the next open."""
        import hermes_cli.models as mod

        with patch.object(mod, "_load_provider_models_cache", return_value={}), \
             patch.object(mod, "_custom_endpoint_fingerprint", return_value="fp"), \
             patch.object(mod, "_save_provider_models_cache") as save, \
             patch.object(mod, "fetch_api_models", return_value=[]):
            out = mod.cached_fetch_api_models("sk-key", "https://gw.example.com/v1")
        assert out == []
        save.assert_not_called()

    def test_blank_base_url_skips_cache_entirely(self):
        """No base_url means nothing to key the cache on — call straight
        through to fetch_api_models rather than caching under an empty key."""
        import hermes_cli.models as mod

        with patch.object(mod, "_load_provider_models_cache") as load, \
             patch.object(mod, "fetch_api_models", return_value=["x"]) as live:
            out = mod.cached_fetch_api_models("sk-key", "")
        assert out == ["x"]
        live.assert_called_once()
        load.assert_not_called()

    def test_fingerprint_ignores_timeout_but_reacts_to_headers(self):
        """Sanity check on the real (non-mocked) fingerprint helper: it must
        not vary with call-only params like timeout, but must vary with the
        actual credential/header inputs."""
        import hermes_cli.models as mod

        fp_a = mod._custom_endpoint_fingerprint("sk-key", None, {"X-Tenant": "a"})
        fp_b = mod._custom_endpoint_fingerprint("sk-key", None, {"X-Tenant": "b"})
        fp_a_again = mod._custom_endpoint_fingerprint("sk-key", None, {"X-Tenant": "a"})
        assert fp_a != fp_b
        assert fp_a == fp_a_again


class TestCachedFetchApiModelsDiskRoundTrip:
    """End-to-end through the real (per-test-isolated) provider_models_cache.json
    disk file rather than mocked load/save, so a regression in the on-disk
    schema (e.g. a key collision with provider-slug entries) would show up
    here even if the mocked unit tests above stayed green."""

    def test_second_call_within_ttl_hits_disk_cache_no_live_fetch(self, monkeypatch):
        import hermes_cli.models as mod

        calls = []

        def fake_fetch(api_key, base_url, **kwargs):
            calls.append((api_key, base_url))
            return ["disk-cached-model"]

        monkeypatch.setattr(mod, "fetch_api_models", fake_fetch)

        first = mod.cached_fetch_api_models("sk-key", "https://gw.example.com/v1")
        second = mod.cached_fetch_api_models("sk-key", "https://gw.example.com/v1")

        assert first == ["disk-cached-model"]
        assert second == ["disk-cached-model"]
        assert len(calls) == 1, "second open must be served from disk, not a fresh live fetch"

    def test_custom_key_does_not_collide_with_provider_slug_cache(self, monkeypatch):
        """A custom endpoint literally named e.g. 'openrouter' in its
        base_url must not read/write the same cache slot as the first-class
        'openrouter' provider slug used by cached_provider_model_ids()."""
        import hermes_cli.models as mod

        monkeypatch.setattr(
            mod, "fetch_api_models", lambda *a, **k: ["custom-endpoint-model"]
        )
        monkeypatch.setattr(
            mod, "provider_model_ids", lambda *a, **k: ["openrouter-curated-model"]
        )

        mod.cached_fetch_api_models("sk-key", "https://openrouter.ai/v1")
        mod.cached_provider_model_ids("openrouter")

        cache = mod._load_provider_models_cache()
        assert cache["custom:https://openrouter.ai/v1"]["models"] == ["custom-endpoint-model"]
        assert cache["openrouter"]["models"] == ["openrouter-curated-model"]


class TestSalvageFollowups:
    """Follow-up behaviors added while salvaging PR #80740: SWR stale-serve
    parity with cached_provider_model_ids, and corrupt-cache degradation."""

    def _entry(self, models, age_seconds, fp="fp"):
        return {"fp": fp, "at": time.time() - age_seconds, "models": list(models)}

    def test_expired_entry_within_stale_window_is_served_and_refreshed_off_thread(self):
        """TTL-expired (but < stale-serve max) entries must be served
        immediately — the picker never blocks on a live round-trip — while a
        background refresh is spawned for the next open."""
        import hermes_cli.models as mod

        cache = {"custom:https://gw.example.com/v1": self._entry(["stale-ok"], age_seconds=7200)}
        with patch.object(mod, "_load_provider_models_cache", return_value=cache), \
             patch.object(mod, "_custom_endpoint_fingerprint", return_value="fp"), \
             patch.object(mod, "_spawn_swr_refresh") as spawn, \
             patch.object(mod, "fetch_api_models") as live:
            out = mod.cached_fetch_api_models(
                "sk-key", "https://gw.example.com/v1", ttl_seconds=3600
            )
        assert out == ["stale-ok"]
        live.assert_not_called()
        spawn.assert_called_once()
        assert spawn.call_args[0][0] == "custom:https://gw.example.com/v1"

    def test_entry_beyond_stale_window_blocks_on_live_fetch(self):
        import hermes_cli.models as mod

        too_old = mod._PROVIDER_MODELS_STALE_SERVE_MAX + 60
        cache = {"custom:https://gw.example.com/v1": self._entry(["ancient"], age_seconds=too_old)}
        with patch.object(mod, "_load_provider_models_cache", return_value=cache), \
             patch.object(mod, "_custom_endpoint_fingerprint", return_value="fp"), \
             patch.object(mod, "_save_provider_models_cache"), \
             patch.object(mod, "_spawn_swr_refresh") as spawn, \
             patch.object(mod, "fetch_api_models", return_value=["fresh"]) as live:
            out = mod.cached_fetch_api_models("sk-key", "https://gw.example.com/v1")
        assert out == ["fresh"]
        live.assert_called_once()
        spawn.assert_not_called()

    def test_swr_refresh_fn_writes_custom_entry_through_shared_scaffolding(self):
        """The generalized _spawn_swr_refresh(key, refresh_fn) must persist a
        custom-endpoint entry via the shared inflight-dedupe machinery."""
        import threading as _threading

        import hermes_cli.models as mod

        done = _threading.Event()
        saved = {}

        def fake_save(data):
            saved.update(data)
            done.set()

        with patch.object(mod, "_load_provider_models_cache", return_value={}), \
             patch.object(mod, "_save_provider_models_cache", side_effect=fake_save):
            mod._spawn_swr_refresh(
                "custom:https://gw.example.com/v1",
                lambda: {"fp": "fp", "at": time.time(), "models": ["refreshed"]},
            )
            assert done.wait(timeout=5), "background refresh did not complete"
        assert saved["custom:https://gw.example.com/v1"]["models"] == ["refreshed"]
        assert "custom:https://gw.example.com/v1" not in mod._swr_refresh_inflight

    def test_corrupt_at_field_degrades_to_live_fetch_instead_of_raising(self):
        """provider_models_cache.json is user-editable; a corrupted 'at' must
        be a cache miss (live fetch), never an exception out of the wrapper."""
        import hermes_cli.models as mod

        cache = {
            "custom:https://gw.example.com/v1": {
                "fp": "fp", "at": "yesterday", "models": ["corrupt-row"],
            }
        }
        with patch.object(mod, "_load_provider_models_cache", return_value=cache), \
             patch.object(mod, "_custom_endpoint_fingerprint", return_value="fp"), \
             patch.object(mod, "_save_provider_models_cache"), \
             patch.object(mod, "fetch_api_models", return_value=["live-models"]) as live:
            out = mod.cached_fetch_api_models("sk-key", "https://gw.example.com/v1")
        assert out == ["live-models"]
        live.assert_called_once()


class TestManagedGatewayCatalogPolicy:
    """托管网关的模型清单是**每用户的权限集合**，不是公开目录。

    上游那套缓存策略的立论是 "model catalogs change on release timescales,
    not hourly" —— 对公开清单成立，对我们这里不成立：管理员在后台点一下，
    这个端点的答案当场就变。

    按默认策略，管理员收回一个模型之后，员工机器上最长还能继续看到它 7 天
    （1 小时 TTL 之后 stale-while-revalidate 会一直端过期条目）。按人给模型
    授权是这个产品的核心，一个要一周才生效的开关等于没有开关。之所以人工验收
    时看着"重新登录就好了"，是因为重新登录会签发新的设备 Key，指纹变了顺带把
    缓存作废 —— 那是只有我们自己知道的隐性知识。
    """

    def _entry(self, models, age_seconds, fp="fp"):
        return {"fp": fp, "at": time.time() - age_seconds, "models": list(models)}

    def _managed(self, mod, origin="http://127.0.0.1:7899"):
        return patch.object(mod, "_managed_gateway_origin", return_value=origin)

    def test_过期条目不再被端出来_而是当场重查(self):
        """这一格红了就意味着被收回授权的模型还会继续出现在选择器里。"""
        import hermes_cli.models as mod

        url = "http://127.0.0.1:7899/v1"
        # 2 小时前：普通 provider 会走 stale-serve 直接端旧的
        cache = {f"custom:{url}": self._entry(["已被收回的模型"], age_seconds=7200)}

        with self._managed(mod), \
             patch.object(mod, "_load_provider_models_cache", return_value=cache), \
             patch.object(mod, "_custom_endpoint_fingerprint", return_value="fp"), \
             patch.object(mod, "_save_provider_models_cache"), \
             patch.object(mod, "_spawn_swr_refresh") as swr, \
             patch.object(mod, "fetch_api_models", return_value=["还留着的模型"]) as live:
            out = mod.cached_fetch_api_models("sk-key", url)

        assert out == ["还留着的模型"]
        live.assert_called_once()
        # 后台刷新那条路专门服务 stale-serve，这里不该走
        swr.assert_not_called()

    def test_一分钟内仍然命中缓存_不给网关来一次请求风暴(self):
        """短 TTL 不等于不缓存：选择器每次打开都打一发是另一种毛病。"""
        import hermes_cli.models as mod

        url = "http://127.0.0.1:7899/v1"
        cache = {f"custom:{url}": self._entry(["m1"], age_seconds=5)}

        with self._managed(mod), \
             patch.object(mod, "_load_provider_models_cache", return_value=cache), \
             patch.object(mod, "_custom_endpoint_fingerprint", return_value="fp"), \
             patch.object(mod, "_save_provider_models_cache"), \
             patch.object(mod, "fetch_api_models") as live:
            out = mod.cached_fetch_api_models("sk-key", url)

        assert out == ["m1"]
        live.assert_not_called()

    def test_cache_only_也不端陈旧的托管清单(self):
        """GUI 选择器为了不卡顿会走 cache_only —— 那条路同样不能端旧权限。"""
        import hermes_cli.models as mod

        url = "http://127.0.0.1:7899/v1"
        cache = {f"custom:{url}": self._entry(["已被收回的模型"], age_seconds=7200)}

        with self._managed(mod), \
             patch.object(mod, "_load_provider_models_cache", return_value=cache), \
             patch.object(mod, "_custom_endpoint_fingerprint", return_value="fp"), \
             patch.object(mod, "fetch_api_models") as live:
            out = mod.cached_fetch_api_models("sk-key", url, cache_only=True)

        assert out is None, "宁可让调用方回落到配置里的清单，也不能端一份过期的权限"
        live.assert_not_called()

    def test_普通自定义端点不受影响_7_天窗口照旧(self):
        """收紧只针对托管网关。把所有自定义端点一起收紧会把 #72762 那类
        「选择器每次打开都卡在一次 HTTP 往返上」的问题放回来。"""
        import hermes_cli.models as mod

        url = "https://gw.example.com/v1"
        cache = {f"custom:{url}": self._entry(["m1"], age_seconds=7200)}

        with self._managed(mod), \
             patch.object(mod, "_load_provider_models_cache", return_value=cache), \
             patch.object(mod, "_custom_endpoint_fingerprint", return_value="fp"), \
             patch.object(mod, "_save_provider_models_cache"), \
             patch.object(mod, "_spawn_swr_refresh") as swr, \
             patch.object(mod, "fetch_api_models") as live:
            out = mod.cached_fetch_api_models("sk-key", url)

        assert out == ["m1"]
        live.assert_not_called()
        swr.assert_called_once()

    def test_stock_hermes_没有托管策略时行为不变(self):
        """非品牌构建里 _managed_gateway_origin() 返回空串，一切照旧。"""
        import hermes_cli.models as mod

        url = "http://127.0.0.1:7899/v1"
        cache = {f"custom:{url}": self._entry(["m1"], age_seconds=7200)}

        with patch.object(mod, "_managed_gateway_origin", return_value=""), \
             patch.object(mod, "_load_provider_models_cache", return_value=cache), \
             patch.object(mod, "_custom_endpoint_fingerprint", return_value="fp"), \
             patch.object(mod, "_save_provider_models_cache"), \
             patch.object(mod, "_spawn_swr_refresh") as swr, \
             patch.object(mod, "fetch_api_models") as live:
            out = mod.cached_fetch_api_models("sk-key", url)

        assert out == ["m1"]
        live.assert_not_called()
        swr.assert_called_once()

    def test_端点判定按前缀且大小写不敏感(self):
        import hermes_cli.models as mod

        with patch.object(mod, "_managed_gateway_origin", return_value="http://127.0.0.1:7899"):
            assert mod._is_managed_catalog_url("http://127.0.0.1:7899/v1")
            assert mod._is_managed_catalog_url("http://127.0.0.1:7899")
            # 别的地址不能沾光：那等于把权限清单的新鲜度让给一个我们不控制的端点
            assert not mod._is_managed_catalog_url("http://127.0.0.1:7898/v1")
            assert not mod._is_managed_catalog_url("https://evil.example.com/v1")

    def test_没有托管策略时任何地址都不算托管(self):
        import hermes_cli.models as mod

        with patch.object(mod, "_managed_gateway_origin", return_value=""):
            assert not mod._is_managed_catalog_url("http://127.0.0.1:7899/v1")
