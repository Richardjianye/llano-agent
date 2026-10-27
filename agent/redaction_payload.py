"""Build a redacted provider request while retaining a private audit copy."""

from __future__ import annotations

import base64
import binascii
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_from_bytes, unquote_to_bytes

from agent.redaction_policy import RedactionPolicy


AUDIT_ENVELOPE_KEY = "_llano_audit"

# Turn kinds tag *why* a request was made, so the audit list can fold the
# machinery (title generation, approval scans) away from what the employee
# actually asked.  The gateway keeps its own copy of this whitelist and
# validates independently — the two sides are separate trust domains and a
# branded client is still just a client (see services/audit_envelope.py).
TURN_KIND_CHAT = "chat"
TURN_KIND_TITLE = "title"
TURN_KIND_APPROVAL = "approval"
TURN_KIND_OTHER = "other"
TURN_KINDS = frozenset(
    {TURN_KIND_CHAT, TURN_KIND_TITLE, TURN_KIND_APPROVAL, TURN_KIND_OTHER}
)

# Hermes session ids look like ``20260821_151706_ac236f`` (agent_init.py).
# Normalizing here keeps junk out of the envelope; it is *not* a security
# boundary — the gateway re-validates and only ever uses the value to group
# rows for display, scoped to one user_uid.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_DATA_URL_RE = re.compile(r"^data:([^;,]*)(;[^,]*)?,(.*)$", re.DOTALL)
_SAFE_TEXT_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "text/csv",
        "text/markdown",
        "text/plain",
        "text/tab-separated-values",
        "text/xml",
    }
)


@dataclass(frozen=True)
class PreparedRedactedRequest:
    payload: dict[str, Any]
    audit_messages: list[Any]
    policy_version: str
    policy: RedactionPolicy
    # Which Hermes session this request belongs to, so the gateway can show a
    # conversation instead of a dozen unrelated rows.  ``None`` when the caller
    # has no session handle — the gateway must keep displaying those records.
    session_id: str | None = None
    turn_kind: str = TURN_KIND_CHAT


def _pattern(policy: RedactionPolicy) -> re.Pattern[str] | None:
    keywords = sorted((value for value in policy.keywords if value), key=len, reverse=True)
    if not keywords:
        return None
    return re.compile("|".join(re.escape(value) for value in keywords), re.IGNORECASE)


def redact_text(text: str, policy: RedactionPolicy) -> str:
    pattern = _pattern(policy)
    return text if pattern is None else pattern.sub(policy.placeholder, text)


def _redact_text_data_url(value: str, policy: RedactionPolicy) -> str:
    match = _DATA_URL_RE.match(value.strip())
    if not match:
        return value
    mime = (match.group(1) or "").strip().lower()
    params = match.group(2) or ""
    encoded = match.group(3)
    if mime not in _SAFE_TEXT_MIME_TYPES:
        return value

    try:
        if "base64" in params.lower():
            cleaned = re.sub(r"\s+", "", encoded)
            raw = base64.b64decode(cleaned + "=" * (-len(cleaned) % 4), validate=False)
        else:
            raw = unquote_to_bytes(encoded)
        text = raw.decode("utf-8")
        redacted = redact_text(text, policy).encode("utf-8")
        if raw == redacted:
            return value
        if "base64" in params.lower():
            replacement = base64.b64encode(redacted).decode("ascii")
        else:
            replacement = quote_from_bytes(redacted, safe="")
        return f"data:{match.group(1)}{params},{replacement}"
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return value


def redact_content(content: Any, policy: RedactionPolicy) -> Any:
    """Redact visible text while preserving multimodal structure and attachments."""
    if isinstance(content, str):
        return redact_text(content, policy)
    result = deepcopy(content)
    if not isinstance(result, list):
        return result
    for part in result:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("text"), str):
            part["text"] = redact_text(part["text"], policy)
        holder = part.get("file")
        if not isinstance(holder, dict):
            continue
        for key in ("file_data", "data"):
            value = holder.get(key)
            if isinstance(value, str):
                holder[key] = _redact_text_data_url(value, policy)
                break
    return result


def redact_messages(messages: list[Any], policy: RedactionPolicy) -> list[Any]:
    result = deepcopy(messages)
    for message in result:
        if isinstance(message, dict) and "content" in message:
            message["content"] = redact_content(message["content"], policy)
    return result


def normalize_session_id(value: Any) -> str | None:
    """Return *value* as a transportable session id, or ``None`` if unusable."""
    text = str(value or "").strip()
    return text if _SESSION_ID_RE.match(text) else None


def normalize_turn_kind(value: Any) -> str:
    """Collapse *value* onto the known turn kinds (unknown → ``other``)."""
    text = str(value or "").strip().lower()
    return text if text in TURN_KINDS else TURN_KIND_OTHER


def prepare_redacted_request(
    request: dict[str, Any],
    policy: RedactionPolicy,
    *,
    session_id: Any = None,
    turn_kind: str = TURN_KIND_CHAT,
) -> PreparedRedactedRequest:
    messages = request.get("messages")
    if not isinstance(messages, list):
        raise ValueError("managed redaction requires a messages payload")

    payload = deepcopy(request)
    audit_messages = deepcopy(messages)
    payload["messages"] = redact_messages(messages, policy)
    return PreparedRedactedRequest(
        payload=payload,
        audit_messages=audit_messages,
        policy_version=policy.version,
        policy=policy,
        session_id=normalize_session_id(session_id),
        turn_kind=normalize_turn_kind(turn_kind),
    )


def prepare_agent_redacted_request(
    agent,
    request: dict[str, Any],
    *,
    turn_kind: str = TURN_KIND_CHAT,
) -> PreparedRedactedRequest | None:
    """Resolve the active branded policy and prepare one provider request."""
    from agent.redaction_policy import resolve_product_redaction_policy

    policy = resolve_product_redaction_policy(
        provider=str(getattr(agent, "provider", "") or ""),
        base_url=str(getattr(agent, "base_url", "") or ""),
        api_key=(
            agent.api_key
            if isinstance(getattr(agent, "api_key", None), str)
            else ""
        ),
    )
    if policy is None:
        return None
    return prepare_redacted_request(
        request,
        policy,
        session_id=getattr(agent, "session_id", None),
        turn_kind=turn_kind,
    )


def attach_audit_envelope(prepared: PreparedRedactedRequest) -> dict[str, Any]:
    payload = dict(prepared.payload)
    existing = payload.get("extra_body")
    if existing is None:
        extra_body: dict[str, Any] = {}
    elif isinstance(existing, dict):
        extra_body = dict(existing)
    else:
        raise ValueError("managed audit envelope requires an object extra_body")
    envelope: dict[str, Any] = {
        "policy_version": prepared.policy_version,
        "messages": deepcopy(prepared.audit_messages),
        "turn_kind": prepared.turn_kind,
    }
    # Omitted rather than sent as null when absent: the gateway treats a
    # missing session id as "cannot be grouped", and an explicit null would
    # only add a second spelling of the same thing.
    if prepared.session_id:
        envelope["session_id"] = prepared.session_id
    extra_body[AUDIT_ENVELOPE_KEY] = envelope
    payload["extra_body"] = extra_body
    return payload
