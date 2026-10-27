"""Managed-endpoint redaction and audit envelope for auxiliary LLM calls.

Auxiliary calls — title generation, command-approval scans, compression,
vision — are ordinary provider requests that carry the employee's content,
but historically they took a different route out of the process than the
conversation loop: ``agent/conversation_loop.py`` applies the branded
redaction policy and attaches the private audit envelope, and auxiliary
calls did neither.  Two consequences, both found by reading real audit rows
on 2026-08-21:

* the gateway's ``mask_messages`` fallback was their *only* redaction layer,
  so decision #9's "client redacts, server backstops" was really "server
  only" for this whole class of traffic;
* with no envelope they carry no session id, so in the audit list they show
  up as unexplained strangers next to the conversation they belong to —
  the "first request always fails" mystery was one of these (a title
  generation), and it cost a database dig to identify.

This module is the single place that fixes both, applied at
``_build_call_kwargs`` — the one function every auxiliary path funnels
through, including the retry and fallback rebuilds.

**Stock Hermes is never touched.**  With no product policy the first check
returns and nothing here runs.  See ``_is_managed_endpoint`` in
``agent/redaction_policy.py`` for why the managed check is on the endpoint
rather than the provider name.

⚠️ **In a branded build an auxiliary call may not leave for anywhere else.**
The auxiliary fallback chain (``fallback_chain`` in the task config) can name
another provider, and a request that lands there would go out unredacted and
unaudited — the last remaining gap of its kind after this module closed the
others.  Decision on 2026-08-23: refuse the call rather than redact it for a
third-party endpoint, which keeps one rule ("branded traffic goes to the
company gateway, full stop") instead of two, and matches the channel
convergence done in 阶段 2.  Config is still the front line — this is the
backstop for when config drifts.

⚠️ **Fail-closed on the managed endpoint** — if the policy cannot be
resolved, the call raises rather than sending unredacted content.  That
matches the conversation loop (decision #9) and costs nothing in practice:
an unresolvable policy means the gateway is unreachable or the credential is
dead, so the main chat is already failing.  It is safe for the two callers
that matter — an approval scan that raises escalates to the human
(``tools/approval.py``), and a title generation that raises just leaves the
session untitled.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class ManagedEndpointRequired(RuntimeError):
    """A branded build tried to send an auxiliary call off the company gateway.

    Raised instead of quietly redacting for a third-party endpoint: redacting
    would still put the employee's content on someone else's server with no
    audit record, and would establish that branded traffic may leave the
    gateway at all.
    """

# Auxiliary task names → the audit's turn_kind vocabulary.  Anything not
# listed is deliberately collapsed to "other" rather than added here on
# sight: turn_kind is a closed whitelist on the gateway too, and a value it
# does not know would land as "other" there anyway.
_TASK_TURN_KINDS = {
    "title_generation": "title",
    "approval": "approval",
}


def turn_kind_for_task(task: Optional[str]) -> str:
    """Map an auxiliary task name onto the audit's turn_kind vocabulary."""
    return _TASK_TURN_KINDS.get(str(task or "").strip().lower(), "other")


def _ambient_session_id() -> Optional[str]:
    """Read the active turn's session id, if the agent loop published one.

    ``agent/aux_accounting.py`` already publishes ``(session_db, session_id)``
    at turn entry for auxiliary token accounting, and the ContextVar
    propagates into worker threads and asyncio tasks.  Reusing it means the
    session id reaches auxiliary calls without threading a parameter through
    every call site — the same reason the accounting context exists.
    """
    try:
        from agent.aux_accounting import get_accounting_context

        context = get_accounting_context()
        if not context:
            return None
        return context[1]
    except Exception:
        return None


def apply_managed_audit_envelope(
    kwargs: Dict[str, Any],
    *,
    provider: str,
    base_url: str,
    api_key: str,
    task: Optional[str] = None,
) -> Dict[str, Any]:
    """Redact an auxiliary request and attach its private audit envelope.

    Returns *kwargs* untouched for stock builds.  In a branded build it
    raises rather than letting an unredacted, unaudited request go out:
    ``ManagedEndpointRequired`` when the destination is not the company
    gateway, ``RedactionPolicyUnavailable`` when it is but the policy cannot
    be resolved.  See the module docstring for why both are hard failures.
    """
    messages = kwargs.get("messages")
    if not isinstance(messages, list) or not messages:
        return kwargs

    from agent.redaction_payload import (attach_audit_envelope,
                                         prepare_redacted_request)
    from agent.redaction_policy import (RedactionPolicyUnavailable,
                                        resolve_product_redaction_policy)

    try:
        policy = resolve_product_redaction_policy(
            provider=str(provider or ""),
            base_url=str(base_url or ""),
            api_key=str(api_key or ""),
        )
    except RedactionPolicyUnavailable as exc:
        # reason == "provider-mismatch" is how the resolver says "this endpoint
        # is not the company gateway".  In a branded build that is not a reason
        # to pass the request through — it is the request we must not send.
        # Every other reason (expired policy, dead credential, gateway
        # unreachable) is a real failure and equally must not be swallowed
        # into an unredacted request.
        #
        # 判据是 reason 字段，不是消息文本。原先在这里 `in str(exc)` 匹配英文
        # 措辞，于是把报错改得好懂一点就会**静默地**改变路由判定 —— 改文案的人
        # 不会想到自己动了安全边界。
        if getattr(exc, "reason", "") == "provider-mismatch":
            raise ManagedEndpointRequired(
                f"auxiliary {task or 'call'} may not leave the company gateway "
                f"(provider={provider!r} base_url={base_url!r})"
            ) from exc
        raise

    if policy is None:
        # Stock build: no product policy, nothing managed, nothing to do.
        return kwargs

    prepared = prepare_redacted_request(
        kwargs,
        policy,
        session_id=_ambient_session_id(),
        turn_kind=turn_kind_for_task(task),
    )
    return attach_audit_envelope(prepared)
