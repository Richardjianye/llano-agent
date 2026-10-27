"""Tests for acp_adapter.session — SessionManager and SessionState."""

import contextlib
import io
import json
import time
from types import SimpleNamespace
import pytest
from unittest.mock import MagicMock, patch

from acp_adapter import session as acp_session
from acp_adapter.session import SessionManager, SessionState
from hermes_state import SessionDB


def _mock_agent():
    return MagicMock(name="MockAIAgent")


@pytest.fixture()
def manager():
    """SessionManager with a mock agent factory (avoids needing API keys)."""
    return SessionManager(agent_factory=_mock_agent)


# ---------------------------------------------------------------------------
# create / get
# ---------------------------------------------------------------------------


class TestCreateSession:
    def test_create_session_returns_state(self, manager):
        state = manager.create_session(cwd="/tmp/work")
        assert isinstance(state, SessionState)
        assert state.cwd == "/tmp/work"
        assert state.session_id
        assert state.history == []
        assert state.agent is not None



    def test_register_task_cwd_translates_windows_drive_for_wsl_tools(self, monkeypatch):
        captured = {}

        def fake_register_task_env_overrides(task_id, overrides):
            captured["task_id"] = task_id
            captured["overrides"] = overrides

        monkeypatch.setattr("hermes_constants._wsl_detected", True)
        monkeypatch.setattr(
            "tools.terminal_tool.register_task_env_overrides",
            fake_register_task_env_overrides,
        )

        acp_session._register_task_cwd("session-1", r"E:\Projects\AI\paperclip")

        assert captured == {
            "task_id": "session-1",
            "overrides": {"cwd": "/mnt/e/Projects/AI/paperclip"},
        }


    def test_get_session(self, manager):
        state = manager.create_session()
        fetched = manager.get_session(state.session_id)
        assert fetched is state


    def test_make_agent_stamps_session_cwd_for_codex_runtime(self, monkeypatch):
        class FakeAgent:
            model = "fake-model"

            def __init__(self, **kwargs):
                self.kwargs = kwargs

        monkeypatch.setattr("run_agent.AIAgent", FakeAgent)
        monkeypatch.setattr(
            "acp_adapter.session.load_config",
            lambda: {
                "model": {
                    "default": "fake-model",
                    "provider": "fake-provider",
                },
                "mcp_servers": {},
            },
            raising=False,
        )
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {
                "model": {
                    "default": "fake-model",
                    "provider": "fake-provider",
                },
                "mcp_servers": {},
            },
        )
        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            lambda requested=None: {
                "provider": requested,
                "api_mode": "codex_app_server",
                "base_url": "https://example.invalid",
                "api_key": "test-key",
            },
        )
        monkeypatch.setattr("acp_adapter.session._register_task_cwd", lambda task_id, cwd: None)

        state = SessionManager(db=None).create_session(cwd="/tmp/project")

        assert state.agent.session_cwd == "/tmp/project"




# ---------------------------------------------------------------------------
# WSL cwd translation
# ---------------------------------------------------------------------------


class TestWslCwdTranslation:
    def test_translate_acp_cwd_converts_windows_drive_path_when_wsl(self, monkeypatch):
        monkeypatch.setattr("hermes_constants._wsl_detected", True)

        assert acp_session._translate_acp_cwd(r"E:\Projects\AI\paperclip") == "/mnt/e/Projects/AI/paperclip"





    def test_fork_session_stores_translated_cwd_on_wsl(self, manager, monkeypatch):
        monkeypatch.setattr("hermes_constants._wsl_detected", True)
        original = manager.create_session(cwd="/tmp/base")

        forked = manager.fork_session(original.session_id, cwd=r"D:\work\project")

        assert forked is not None
        assert forked.cwd == "/mnt/d/work/project"

    def test_update_cwd_stores_translated_cwd_on_wsl(self, manager, monkeypatch):
        monkeypatch.setattr("hermes_constants._wsl_detected", True)
        state = manager.create_session(cwd="/tmp/old")

        updated = manager.update_cwd(state.session_id, cwd=r"C:\Users\foo\project")

        assert updated is not None
        assert updated.cwd == "/mnt/c/Users/developer/project"

# ---------------------------------------------------------------------------
# fork
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# list / cleanup / remove
# ---------------------------------------------------------------------------


class TestListAndCleanup:
    def test_list_sessions_empty(self, manager):
        assert manager.list_sessions() == []



    def test_save_session_preserves_existing_messages_on_encode_failure(self, manager):
        """Regression for #13675: a bad message in state.history must not
        clobber the previously-persisted transcript.  replace_messages()
        wraps DELETE + INSERT in a single rolled-back-on-exception txn.
        """
        state = manager.create_session()
        state.history.append({"role": "user", "content": "original"})
        manager.save_session(state.session_id)

        # Now swap history with a message whose tool_calls is non-JSON-serializable.
        # _execute_write rolls back; the previously persisted "original" stays.
        state.history = [
            {"role": "user", "content": "replacement"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"bad": object()}],
            },
        ]
        manager.save_session(state.session_id)

        db = manager._get_db()
        messages = db.get_messages_as_conversation(state.session_id)
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "original"
        assert isinstance(messages[0].get("timestamp"), (int, float))




    def test_cleanup_clears_all(self, manager):
        s1 = manager.create_session()
        s2 = manager.create_session()
        s1.history.append({"role": "user", "content": "one"})
        s2.history.append({"role": "user", "content": "two"})
        assert len(manager.list_sessions()) == 2
        manager.cleanup()
        assert manager.list_sessions() == []

    def test_remove_session(self, manager):
        state = manager.create_session()
        assert manager.remove_session(state.session_id) is True
        assert manager.get_session(state.session_id) is None
        # Removing again returns False
        assert manager.remove_session(state.session_id) is False


# ---------------------------------------------------------------------------
# persistence — sessions survive process restarts (via SessionDB)
# ---------------------------------------------------------------------------


class TestPersistence:
    """Verify that sessions are persisted to SessionDB and can be restored."""

    def test_persist_merges_prompt_policy_and_never_erases_it(self, tmp_path):
        from agent.managed_prompt_policy import (
            PROMPT_POLICY_MODEL_CONFIG_KEY,
            prompt_policy_content_hash,
        )

        policy = {
            "version": "11",
            "content_hash": prompt_policy_content_hash("CORE", "PERSONA"),
            "company_core_policy": "CORE",
            "default_persona": "PERSONA",
            "published_at": "2026-09-17T00:00:00Z",
            "source": "gateway",
        }
        agent = SimpleNamespace(
            model="custom-model",
            provider="custom",
            base_url="https://models.example/v1",
            api_mode="chat_completions",
            _session_init_model_config={
                PROMPT_POLICY_MODEL_CONFIG_KEY: policy
            },
            _cached_system_prompt="EXACT ACP SYSTEM PROMPT",
            _session_db=None,
            _session_db_created=False,
        )
        db = SessionDB(tmp_path / "state.db")
        manager = SessionManager(agent_factory=lambda: agent, db=db)
        state = manager.create_session(cwd="/work")

        row = db.get_session(state.session_id)
        config = json.loads(row["model_config"])
        assert config[PROMPT_POLICY_MODEL_CONFIG_KEY] == policy
        assert row["system_prompt"] == "EXACT ACP SYSTEM PROMPT"

        # A later save from a lightweight/recreated agent with no staged
        # policy must merge the existing envelope, not replace it with cwd.
        agent._session_init_model_config = {}
        state.cwd = "/work/renamed"
        manager.save_session(state.session_id)

        saved = db.get_session(state.session_id)
        saved_config = json.loads(saved["model_config"])
        assert saved_config[PROMPT_POLICY_MODEL_CONFIG_KEY] == policy
        assert saved_config["cwd"] == "/work/renamed"
        db.close()

    def test_fork_inherits_custom_runtime_policy_exact_prompt_and_static_prefix(
        self, tmp_path
    ):
        from agent.managed_prompt_policy import (
            PROMPT_POLICY_MODEL_CONFIG_KEY,
            prompt_policy_content_hash,
            snapshot_from_payload,
        )

        payload = {
            "version": 12,
            "content_hash": prompt_policy_content_hash("CORE", "PERSONA"),
            "company_core_policy": "CORE",
            "default_persona": "PERSONA",
            "published_at": "2026-09-17T00:00:00Z",
        }
        snapshot = snapshot_from_payload(payload, source="gateway")
        policy = snapshot.to_model_config()
        parent_agent = SimpleNamespace(
            model="vendor/model-v2",
            requested_provider="custom:corp",
            provider="custom",
            base_url="https://corp-models.example/v1",
            api_mode="chat_completions",
            reasoning_config={"enabled": True, "effort": "high"},
            service_tier="priority",
            _prompt_policy_snapshot=snapshot,
            _session_init_model_config={
                PROMPT_POLICY_MODEL_CONFIG_KEY: policy
            },
            _cached_system_prompt="STATIC PREFIX\n\nEXACT REST",
            _cached_system_prompt_static="STATIC PREFIX",
            _session_db=None,
            _session_db_created=False,
        )

        class RecordingManager(SessionManager):
            calls = []

            def _make_agent(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(
                    model=kwargs.get("model"),
                    requested_provider=kwargs.get("requested_provider"),
                    provider="custom",
                    base_url=kwargs.get("base_url"),
                    api_mode=kwargs.get("api_mode"),
                    reasoning_config=kwargs.get("reasoning_config"),
                    service_tier=kwargs.get("service_tier"),
                    _session_init_model_config={},
                    _cached_system_prompt=None,
                    _cached_system_prompt_static=None,
                    _session_db=None,
                    _session_db_created=False,
                )

        db = SessionDB(tmp_path / "state.db")
        manager = RecordingManager(db=db)
        parent = SessionState(
            session_id="parent",
            agent=parent_agent,
            cwd="/work",
            model=parent_agent.model,
            history=[{"role": "user", "content": "hello"}],
        )
        manager._sessions[parent.session_id] = parent
        manager._persist(parent)

        forked = manager.fork_session("parent", cwd="/fork")

        assert forked is not None
        build = manager.calls[-1]
        assert build["parent_session_id"] == "parent"
        assert build["prompt_policy_snapshot"] is snapshot
        assert build["requested_provider"] == "custom:corp"
        assert build["base_url"] == "https://corp-models.example/v1"
        assert build["api_mode"] == "chat_completions"
        assert build["reasoning_config"] == {"enabled": True, "effort": "high"}
        assert build["service_tier"] == "priority"
        assert forked.agent._cached_system_prompt == "STATIC PREFIX\n\nEXACT REST"
        assert forked.agent._cached_system_prompt_static == "STATIC PREFIX"

        row = db.get_session(forked.session_id)
        config = json.loads(row["model_config"])
        assert row["parent_session_id"] == "parent"
        assert row["system_prompt"] == "STATIC PREFIX\n\nEXACT REST"
        assert config[PROMPT_POLICY_MODEL_CONFIG_KEY] == policy
        assert config["_branched_from"] == "parent"

        manager._sessions.pop(forked.session_id)
        restored = manager.get_session(forked.session_id)
        assert restored is not None
        assert restored.parent_session_id == "parent"
        manager.save_session(forked.session_id)
        restarted_config = json.loads(
            db.get_session(forked.session_id)["model_config"]
        )
        assert restarted_config[PROMPT_POLICY_MODEL_CONFIG_KEY] == policy
        db.close()














    def test_only_restores_acp_sessions(self, manager):
        """get_session should not restore non-ACP sessions from DB."""
        db = manager._get_db()
        # Manually create a CLI session in the DB.
        db.create_session(session_id="cli-session-123", source="cli", model="test")
        # Should not be found via ACP SessionManager.
        assert manager.get_session("cli-session-123") is None

    def test_sessions_searchable_via_fts(self, manager):
        """ACP sessions stored in SessionDB are searchable via FTS5."""
        state = manager.create_session()
        state.history.append({"role": "user", "content": "how do I configure nginx"})
        state.history.append({"role": "assistant", "content": "Here is the nginx config..."})
        manager.save_session(state.session_id)

        db = manager._get_db()
        results = db.search_messages("nginx")
        assert len(results) > 0
        session_ids = {r["session_id"] for r in results}
        assert state.session_id in session_ids


    def test_assistant_reasoning_fields_persisted(self, manager):
        """ACP session restore should preserve assistant reasoning context."""
        state = manager.create_session()
        state.history.append({
            "role": "assistant",
            "content": "hello",
            "reasoning": "step-by-step",
            "reasoning_details": [
                {"type": "thinking", "thinking": "first thought"},
            ],
            "codex_reasoning_items": [
                {"type": "reasoning", "id": "rs_123", "encrypted_content": "enc_blob"},
            ],
        })
        manager.save_session(state.session_id)

        with manager._lock:
            del manager._sessions[state.session_id]

        restored = manager.get_session(state.session_id)
        assert restored is not None
        msg = restored.history[0]
        assert isinstance(msg.pop("timestamp", None), (int, float))
        assert restored.history == [{
            "role": "assistant",
            "content": "hello",
            "reasoning": "step-by-step",
            "reasoning_details": [
                {"type": "thinking", "thinking": "first thought"},
            ],
            "codex_reasoning_items": [
                {"type": "reasoning", "id": "rs_123", "encrypted_content": "enc_blob"},
            ],
        }]


    def test_acp_agents_route_human_output_to_stderr(self, tmp_path, monkeypatch):
        """ACP agents must keep stdout clean for JSON-RPC stdio transport."""

        def fake_resolve_runtime_provider(requested=None, **kwargs):
            return {
                "provider": "openrouter",
                "api_mode": "chat_completions",
                "base_url": "https://openrouter.example/v1",
                "api_key": "test-key",
                "command": None,
                "args": [],
            }

        def fake_agent(**kwargs):
            return SimpleNamespace(model=kwargs.get("model"), _print_fn=None)

        monkeypatch.setattr("hermes_cli.config.load_config", lambda: {
            "model": {"provider": "openrouter", "default": "test-model"}
        })
        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            fake_resolve_runtime_provider,
        )
        db = SessionDB(tmp_path / "state.db")

        with patch("run_agent.AIAgent", side_effect=fake_agent):
            manager = SessionManager(db=db)
            state = manager.create_session(cwd="/work")

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            state.agent._print_fn("ACP noise")

        assert stdout_buf.getvalue() == ""
        assert stderr_buf.getvalue() == "ACP noise\n"
