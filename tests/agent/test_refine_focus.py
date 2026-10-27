"""Tests for the /refine focus parameter on spawn_background_review_thread."""

from unittest.mock import MagicMock

from agent.background_review import (
    _COMBINED_REVIEW_PROMPT,
    _MEMORY_REVIEW_PROMPT,
    spawn_background_review_thread,
)


def _bare_agent():
    agent = MagicMock()
    # Ensure getattr(agent, "_COMBINED_REVIEW_PROMPT", default) hits the default.
    del agent._COMBINED_REVIEW_PROMPT
    del agent._MEMORY_REVIEW_PROMPT
    del agent._SKILL_REVIEW_PROMPT
    return agent


def test_no_focus_prompt_is_byte_identical():
    agent = _bare_agent()
    _target, prompt = spawn_background_review_thread(
        agent, [], review_memory=True, review_skills=True
    )
    assert prompt == _COMBINED_REVIEW_PROMPT

    _target, prompt = spawn_background_review_thread(
        agent, [], review_memory=True, review_skills=True, focus=None
    )
    assert prompt == _COMBINED_REVIEW_PROMPT

    _target, prompt = spawn_background_review_thread(
        agent, [], review_memory=True, review_skills=True, focus="   "
    )
    assert prompt == _COMBINED_REVIEW_PROMPT


def test_focus_is_appended_to_prompt():
    agent = _bare_agent()
    _target, prompt = spawn_background_review_thread(
        agent, [], review_memory=True, review_skills=True,
        focus="save the deploy workflow as a skill",
    )
    assert prompt.startswith(_COMBINED_REVIEW_PROMPT)
    assert "save the deploy workflow as a skill" in prompt
    assert "explicitly requested" in prompt


def test_focus_works_with_memory_only_prompt():
    agent = _bare_agent()
    _target, prompt = spawn_background_review_thread(
        agent, [], review_memory=True, review_skills=False, focus="remember my timezone",
    )
    assert prompt.startswith(_MEMORY_REVIEW_PROMPT)
    assert "remember my timezone" in prompt


def test_branded_skill_reviews_carry_the_publishing_standard(monkeypatch):
    import agent.background_review as review
    from tools.skill_publish_spec import AUTHORING_SPEC_EN

    monkeypatch.setattr(review, "_branded_runtime", lambda: True)
    agent = _bare_agent()
    _target, prompt = spawn_background_review_thread(agent, [], review_memory=True, review_skills=True)
    assert prompt.startswith(_COMBINED_REVIEW_PROMPT) and AUTHORING_SPEC_EN in prompt

    _target, prompt = spawn_background_review_thread(agent, [], review_memory=True, review_skills=False)
    assert prompt == _MEMORY_REVIEW_PROMPT, "只复盘记忆时不追加技能规范"
