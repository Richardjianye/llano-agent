"""Session-frozen managed prompt policy for branded runtimes.

The network is consulted only while a genuinely new session is initialized.
Prompt assembly is deliberately pure: it reads the immutable snapshot already
attached to the agent and never fetches or refreshes policy mid-conversation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from hermes_constants import get_hermes_home
from utils import atomic_write_text

logger = logging.getLogger(__name__)

PROMPT_POLICY_MODEL_CONFIG_KEY = "prompt_policy_snapshot"
PROMPT_POLICY_CACHE_FILENAME = "managed-prompt-policy.json"
PROMPT_POLICY_OBJECT = "llano.prompt_policy"
MAX_COMPANY_POLICY_CHARS = 32_000
MAX_DEFAULT_PERSONA_CHARS = 8_000
MAX_POLICY_BYTES = 128_000
# Transport envelope cap. The semantic 128,000-byte limit applies only to the
# normalized core+persona content; JSON keys and escaping add overhead, so the
# bounded HTTP read must be safely larger while still preventing unbounded I/O.
MAX_POLICY_RESPONSE_BYTES = 512 * 1024
_FETCH_TIMEOUT_SECONDS = 4.0

FetchJson = Callable[[str, dict[str, str]], dict[str, Any]]
_UNSET = object()
_CACHE_THREAD_LOCKS_GUARD = threading.Lock()
_CACHE_THREAD_LOCKS: dict[str, threading.Lock] = {}


@dataclass(frozen=True)
class _InvalidManagedProductScope:
    """Fail-closed scope used when declared managed config is unreadable."""

    brand_id: str = "invalid-managed-product"
    environment: str = "invalid"
    gateway_origin: str = ""


def _resolve_product_policy(value: object) -> tuple[object | None, bool]:
    """Return (scope, managed) without collapsing INVALID into stock."""
    if value is not _UNSET:
        return value, value is not None
    from hermes_cli.product_policy import (
        ProductPolicyState,
        load_product_policy_status,
    )

    status = load_product_policy_status()
    if status.state is ProductPolicyState.STOCK:
        return None, False
    if status.state is ProductPolicyState.ACTIVE:
        return status.policy, True
    logger.error(
        "Managed product policy is invalid; retaining managed prompt "
        "governance with local safe fallback: %s",
        status.reason,
    )
    return _InvalidManagedProductScope(), True


class PromptPolicyUnpublished(LookupError):
    """The gateway explicitly says no managed policy has been published.

    This is deliberately distinct from an unavailable or malformed gateway:
    an explicit unpublished response selects the bundled baseline immediately,
    while availability failures retain the last-known-good policy.
    """


def _normalize_content(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def prompt_policy_content_hash(
    company_core_policy: str, default_persona: str
) -> str:
    """Return the gateway/client canonical semantic content hash."""
    semantic = {
        "company_core_policy": _normalize_content(company_core_policy),
        "default_persona": _normalize_content(default_persona),
    }
    encoded = json.dumps(
        semantic,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True)
class PromptPolicySnapshot:
    version: str
    content_hash: str
    company_core_policy: str
    default_persona: str
    published_at: str
    source: str

    def to_model_config(self) -> dict[str, str]:
        return {
            "version": self.version,
            "content_hash": self.content_hash,
            "company_core_policy": self.company_core_policy,
            "default_persona": self.default_persona,
            "published_at": self.published_at,
            "source": self.source,
        }


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("prompt policy timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def snapshot_from_payload(
    payload: object,
    *,
    source: str,
    enforce_expiry: bool = True,
    now: datetime | None = None,
) -> PromptPolicySnapshot:
    """Validate and normalize a gateway, LKG, or model-config snapshot."""
    if not isinstance(payload, dict):
        raise ValueError("prompt policy response is not an object")

    version = payload.get("version")
    content_hash = payload.get("content_hash")
    core = payload.get("company_core_policy")
    persona = payload.get("default_persona")
    published_at = payload.get("published_at", "")
    # Gateway versions are backed by a BigInteger and therefore arrive as JSON
    # numbers.  Keep accepting non-empty string versions as well so bundled and
    # future opaque version schemes remain compatible, but reject booleans
    # (``bool`` is an ``int`` subclass) and non-positive numeric versions.
    if isinstance(version, bool):
        raise ValueError("invalid prompt policy version")
    if isinstance(version, int):
        if version <= 0:
            raise ValueError("invalid prompt policy version")
        normalized_version = str(version)
    elif isinstance(version, str) and version.strip():
        normalized_version = version.strip()
    else:
        raise ValueError("invalid prompt policy version")
    if len(normalized_version) > 256:
        raise ValueError("invalid prompt policy version")
    if not isinstance(content_hash, str):
        raise ValueError("invalid prompt policy content_hash")
    if not isinstance(core, str) or not isinstance(persona, str):
        raise ValueError("prompt policy content must be text")
    if not isinstance(published_at, str) or len(published_at) > 128:
        raise ValueError("invalid prompt policy published_at")

    core = _normalize_content(core)
    persona = _normalize_content(persona)
    if not core or not persona:
        raise ValueError("prompt policy content cannot be empty")
    if "\x00" in core or "\x00" in persona:
        raise ValueError("prompt policy contains NUL")
    if len(core) > MAX_COMPANY_POLICY_CHARS:
        raise ValueError("company prompt policy is too large")
    if len(persona) > MAX_DEFAULT_PERSONA_CHARS:
        raise ValueError("default persona is too large")
    try:
        content_bytes = (core + persona).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("prompt policy content is not valid UTF-8") from exc
    if len(content_bytes) > MAX_POLICY_BYTES:
        raise ValueError("prompt policy byte size is too large")

    expected_hash = prompt_policy_content_hash(core, persona)
    if content_hash.strip().lower() != expected_hash:
        raise ValueError("prompt policy content_hash mismatch")

    expires_at = payload.get("expires_at")
    if enforce_expiry and expires_at is not None:
        if not isinstance(expires_at, str):
            raise ValueError("invalid prompt policy expires_at")
        if _parse_timestamp(expires_at) <= (now or datetime.now(timezone.utc)):
            raise ValueError("prompt policy response is expired")

    return PromptPolicySnapshot(
        version=normalized_version,
        content_hash=expected_hash,
        company_core_policy=core,
        default_persona=persona,
        published_at=published_at.strip(),
        source=source,
    )


def _legacy_snapshot() -> PromptPolicySnapshot:
    """Pin a pre-feature conversation without adding rules mid-session."""
    return PromptPolicySnapshot(
        version="legacy:no-managed-policy",
        content_hash=prompt_policy_content_hash("", ""),
        company_core_policy="",
        default_persona="",
        published_at="",
        source="legacy-session",
    )


def _bundled_snapshot() -> PromptPolicySnapshot:
    from hermes_cli.default_prompt_policy import (
        BUNDLED_COMPANY_CORE_POLICY,
        BUNDLED_DEFAULT_PERSONA,
        BUNDLED_PROMPT_POLICY_VERSION,
    )
    core = _normalize_content(BUNDLED_COMPANY_CORE_POLICY)
    persona = _normalize_content(BUNDLED_DEFAULT_PERSONA)
    return PromptPolicySnapshot(
        version=BUNDLED_PROMPT_POLICY_VERSION,
        content_hash=prompt_policy_content_hash(core, persona),
        company_core_policy=core,
        default_persona=persona,
        published_at="",
        source="bundled",
    )


def bundled_prompt_policy_snapshot() -> PromptPolicySnapshot:
    """Public safe baseline for a declared-but-invalid managed product."""
    return _bundled_snapshot()


def effective_prefill_messages(agent: Any) -> list[dict[str, Any]]:
    """Return caller-configured prefills only for stock sessions.

    ``prefill_messages_file`` is user-controlled and its entries are inserted
    immediately after the system message on both the main and summary request
    paths.  Once a managed snapshot exists, no such entry may acquire message
    role authority (including via a later in-memory mutation), so managed
    sessions suppress the entire optional prefill surface.  Stock Hermes keeps
    its existing behavior unchanged.
    """
    if getattr(agent, "_prompt_policy_snapshot", None) is not None:
        return []
    value = getattr(agent, "prefill_messages", None)
    return value if isinstance(value, list) else []


def _read_bounded_json(response: Any) -> dict[str, Any]:
    """Read at most one byte beyond the public response-size contract."""
    body = response.read(MAX_POLICY_RESPONSE_BYTES + 1)
    if len(body) > MAX_POLICY_RESPONSE_BYTES:
        raise ValueError("prompt policy response is too large")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("prompt policy response is not valid JSON UTF-8") from exc
    if not isinstance(payload, dict):
        raise ValueError("prompt policy response is not an object")
    return payload


def _is_unpublished_response(payload: object) -> bool:
    if not isinstance(payload, dict) or payload.get("code") != 10011:
        return False
    data = payload.get("data")
    return isinstance(data, dict) and data.get(
        "reason"
    ) == "prompt_policy_unpublished" and data.get("fallback") == "bundled"


def _fetch_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310
            return _read_bounded_json(response)
    except HTTPError as exc:
        # Only the gateway's exact unpublished contract bypasses an older LKG.
        # A generic/invalid 404 is an availability failure like 5xx and keeps
        # the normal LKG -> bundled fallback chain.
        if exc.code == 404:
            try:
                payload = _read_bounded_json(exc)
            finally:
                exc.close()
            if _is_unpublished_response(payload):
                raise PromptPolicyUnpublished(
                    "gateway prompt policy is unpublished"
                ) from exc
        raise


def _cache_path(home: Path) -> Path:
    return home / "cache" / PROMPT_POLICY_CACHE_FILENAME


def _normalized_gateway_origin(value: object) -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return ""
    parts = urlsplit(raw)
    if not parts.scheme or not parts.hostname:
        return raw
    host = parts.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    try:
        port = parts.port
    except ValueError:
        return raw
    if port is not None:
        netloc += f":{port}"
    return urlunsplit(
        (parts.scheme.lower(), netloc, parts.path.rstrip("/"), "", "")
    )


def _policy_scope(product_policy: object) -> dict[str, str]:
    """Return the exact branded environment identity bound to an LKG."""
    return {
        "brand_id": str(getattr(product_policy, "brand_id", "") or "")
        .strip()
        .lower(),
        "environment": str(getattr(product_policy, "environment", "") or "")
        .strip()
        .lower(),
        "gateway_origin": _normalized_gateway_origin(
            getattr(product_policy, "gateway_origin", "")
        ),
    }


def _load_lkg(
    home: Path, *, expected_scope: dict[str, str]
) -> PromptPolicySnapshot | None:
    path = _cache_path(home)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        # Caches predating environment binding are unsafe when one HERMES_HOME
        # is reused across test/staging/production, so ignore rather than
        # guessing their provenance.
        if payload.get("scope") != expected_scope:
            raise ValueError("prompt policy cache scope mismatch")
        # LKG has no availability expiry. Optional transport expiry is checked
        # only when the gateway response first arrives.
        return snapshot_from_payload(
            payload, source="lkg", enforce_expiry=False
        )
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 - damaged cache falls back safely
        logger.warning("Ignoring invalid managed prompt-policy cache %s: %s", path, exc)
        return None


def _write_lkg(
    home: Path,
    snapshot: PromptPolicySnapshot,
    *,
    scope: dict[str, str],
) -> None:
    payload = snapshot.to_model_config()
    payload["object"] = PROMPT_POLICY_OBJECT
    payload["scope"] = scope
    atomic_write_text(
        _cache_path(home),
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        create_mode=0o600,
    )


@contextmanager
def _lkg_update_lock(home: Path):
    """Serialize LKG compare-and-replace across threads and processes."""
    lock_path = _cache_path(home).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    key = str(lock_path)
    with _CACHE_THREAD_LOCKS_GUARD:
        thread_lock = _CACHE_THREAD_LOCKS.setdefault(key, threading.Lock())
    with thread_lock:
        with open(lock_path, "a+b") as handle:
            if os.name == "nt":
                import msvcrt

                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _monotonic_remote_snapshot(
    remote: PromptPolicySnapshot,
    cached: PromptPolicySnapshot | None,
) -> PromptPolicySnapshot:
    """Keep a scoped LKG when a lagging or split-brain replica responds.

    Published gateway versions are monotonically increasing integers.  A
    deliberate rollback is therefore represented by a newer version whose
    content happens to match an older policy, never by lowering the version.
    Opaque future version schemes remain accepted, but an identical version
    must always name identical content.
    """
    if cached is None:
        return remote
    if remote.version == cached.version:
        if remote.content_hash != cached.content_hash:
            logger.error(
                "Rejecting split-brain managed prompt policy version %s; "
                "keeping last-known-good content",
                remote.version,
            )
            return cached
        return remote
    try:
        remote_version = int(remote.version)
        cached_version = int(cached.version)
    except (TypeError, ValueError):
        return remote
    if remote_version < cached_version:
        logger.warning(
            "Rejecting managed prompt-policy rollback %s -> %s; keeping "
            "last-known-good content",
            cached.version,
            remote.version,
        )
        return cached
    return remote


def _install_remote_lkg(
    home: Path,
    remote: PromptPolicySnapshot,
    *,
    scope: dict[str, str],
) -> PromptPolicySnapshot:
    """Compare against the latest on-disk value under the write lock."""
    with _lkg_update_lock(home):
        current = _load_lkg(home, expected_scope=scope)
        selected = _monotonic_remote_snapshot(remote, current)
        if selected is remote:
            _write_lkg(home, remote, scope=scope)
        return selected


def resolve_latest_product_prompt_policy(
    *,
    api_key: str,
    product_policy: object = _UNSET,
    hermes_home: Path | None = None,
    fetch_json: FetchJson = _fetch_json,
) -> PromptPolicySnapshot | None:
    """Resolve a new session's policy: gateway -> LKG -> bundled baseline."""
    product_policy, managed = _resolve_product_policy(product_policy)
    if not managed:
        return None  # Stock Hermes: no network and no company prompt injection.

    home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    scope = _policy_scope(product_policy)
    cached = _load_lkg(home, expected_scope=scope)
    origin = scope["gateway_origin"]
    if origin and isinstance(api_key, str) and api_key:
        try:
            payload = fetch_json(
                f"{origin}/v1/prompt-policy/effective",
                {
                    "Accept": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            remote = snapshot_from_payload(payload, source="gateway")
            return _install_remote_lkg(home, remote, scope=scope)
        except PromptPolicyUnpublished:
            return _bundled_snapshot()
        except Exception as exc:  # noqa: BLE001 - availability falls through to LKG
            logger.warning(
                "Managed prompt-policy refresh failed; using local fallback: %s",
                exc,
            )

    return cached if cached is not None else _bundled_snapshot()


def _model_config(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}
    return {}


def _snapshot_from_session_row(row: object) -> PromptPolicySnapshot | None:
    if not isinstance(row, dict):
        return None
    payload = _model_config(row.get("model_config")).get(
        PROMPT_POLICY_MODEL_CONFIG_KEY
    )
    if not isinstance(payload, dict):
        return None
    if payload.get("version") == "legacy:no-managed-policy":
        return _legacy_snapshot()
    try:
        return snapshot_from_payload(
            payload,
            source=str(payload.get("source") or "session"),
            enforce_expiry=False,
        )
    except ValueError as exc:
        logger.warning("Ignoring invalid session prompt-policy snapshot: %s", exc)
        return None


def initialize_agent_prompt_policy(
    agent: Any,
    *,
    product_policy: object = _UNSET,
) -> PromptPolicySnapshot | None:
    """Attach one immutable snapshot to an agent before prompt construction."""
    product_policy, managed = _resolve_product_policy(product_policy)
    if not managed:
        agent._prompt_policy_snapshot = None
        return None

    inherited = getattr(agent, "_inherited_prompt_policy_snapshot", None)
    if isinstance(inherited, PromptPolicySnapshot):
        snapshot = inherited
    else:
        snapshot = None

    db = getattr(agent, "_session_db", None)
    session_id = str(getattr(agent, "session_id", "") or "")
    row = None
    if db is not None and session_id and callable(getattr(db, "get_session", None)):
        try:
            row = db.get_session(session_id)
        except Exception as exc:  # noqa: BLE001 - fresh-session fallback remains available
            logger.warning("Could not read session prompt-policy snapshot: %s", exc)

    if snapshot is None:
        snapshot = _snapshot_from_session_row(row)

    # A stored prompt is an existing logical conversation. If it predates this
    # feature, pin "no managed policy" rather than introducing rules on resume.
    if snapshot is None and isinstance(row, dict) and row.get("system_prompt"):
        snapshot = _legacy_snapshot()

    if snapshot is None:
        raw_key = getattr(agent, "api_key", "")
        api_key = raw_key if isinstance(raw_key, str) else ""
        snapshot = resolve_latest_product_prompt_policy(
            api_key=api_key,
            product_policy=product_policy,
        )

    agent._prompt_policy_snapshot = snapshot
    if snapshot is None:
        return None

    config = getattr(agent, "_session_init_model_config", None)
    if not isinstance(config, dict):
        config = {}
        agent._session_init_model_config = config
    serialized = snapshot.to_model_config()
    config[PROMPT_POLICY_MODEL_CONFIG_KEY] = serialized

    if isinstance(row, dict) and callable(
        getattr(db, "patch_session_model_config", None)
    ):
        try:
            db.patch_session_model_config(
                session_id, {PROMPT_POLICY_MODEL_CONFIG_KEY: serialized}
            )
        except Exception as exc:  # noqa: BLE001 - run_agent create path retries persistence
            logger.warning("Could not persist session prompt-policy snapshot: %s", exc)

    return snapshot


def refresh_agent_prompt_policy_for_new_session(
    agent: Any,
    *,
    product_policy: object = _UNSET,
) -> PromptPolicySnapshot | None:
    """Resolve and stage a brand-new conversation's latest validated policy."""
    product_policy, managed = _resolve_product_policy(product_policy)

    config = getattr(agent, "_session_init_model_config", None)
    if not isinstance(config, dict):
        config = {}
        agent._session_init_model_config = config
    config.pop(PROMPT_POLICY_MODEL_CONFIG_KEY, None)
    agent._inherited_prompt_policy_snapshot = None
    agent._parent_session_id = None

    if not managed:
        snapshot = None
    else:
        raw_key = getattr(agent, "api_key", "")
        snapshot = resolve_latest_product_prompt_policy(
            api_key=raw_key if isinstance(raw_key, str) else "",
            product_policy=product_policy,
        )
    agent._prompt_policy_snapshot = snapshot
    if snapshot is not None:
        config[PROMPT_POLICY_MODEL_CONFIG_KEY] = snapshot.to_model_config()
    agent._cached_system_prompt = None
    agent._cached_system_prompt_static = None
    return snapshot


def restore_agent_prompt_policy_for_session(
    agent: Any,
    session_row: object,
    *,
    product_policy: object = _UNSET,
) -> PromptPolicySnapshot | None:
    """Adopt a resumed session's frozen snapshot and exact prompt bytes."""
    product_policy, managed = _resolve_product_policy(product_policy)

    config = getattr(agent, "_session_init_model_config", None)
    if not isinstance(config, dict):
        config = {}
        agent._session_init_model_config = config
    config.pop(PROMPT_POLICY_MODEL_CONFIG_KEY, None)
    agent._inherited_prompt_policy_snapshot = None

    # An explicit durable snapshot is the conversation's identity, even if
    # the current process can no longer prove the product scope (or an old
    # test/host starts stock).  Resuming must not silently strip or replace it.
    # Fresh stock sessions still never resolve or inject managed policy.
    snapshot = _snapshot_from_session_row(session_row)
    if snapshot is not None:
        managed = True
    stored_prompt = (
        session_row.get("system_prompt")
        if isinstance(session_row, dict)
        else None
    )
    if snapshot is None and managed:
        if isinstance(stored_prompt, str) and stored_prompt:
            snapshot = _legacy_snapshot()
        else:
            raw_key = getattr(agent, "api_key", "")
            snapshot = resolve_latest_product_prompt_policy(
                api_key=raw_key if isinstance(raw_key, str) else "",
                product_policy=product_policy,
            )

    agent._prompt_policy_snapshot = snapshot
    if snapshot is not None:
        config[PROMPT_POLICY_MODEL_CONFIG_KEY] = snapshot.to_model_config()

    agent._cached_system_prompt = (
        stored_prompt if isinstance(stored_prompt, str) and stored_prompt else None
    )
    agent._cached_system_prompt_static = None
    agent._static_rebuild_failed_for = None
    if agent._cached_system_prompt:
        from agent.system_prompt import reconstruct_static_prefix

        reconstruct_static_prefix(agent, log_label="CLI resume")
    return snapshot


__all__ = [
    "MAX_COMPANY_POLICY_CHARS",
    "MAX_DEFAULT_PERSONA_CHARS",
    "MAX_POLICY_RESPONSE_BYTES",
    "PROMPT_POLICY_CACHE_FILENAME",
    "PROMPT_POLICY_MODEL_CONFIG_KEY",
    "PromptPolicySnapshot",
    "PromptPolicyUnpublished",
    "bundled_prompt_policy_snapshot",
    "effective_prefill_messages",
    "initialize_agent_prompt_policy",
    "prompt_policy_content_hash",
    "refresh_agent_prompt_policy_for_new_session",
    "resolve_latest_product_prompt_policy",
    "restore_agent_prompt_policy_for_session",
    "snapshot_from_payload",
]
