"""DingTalk: pairing by default (opt-in open access), inbound media caching,
and outbound file/image delivery through the robot OpenAPI."""
import json
import os
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch):
    for key in (
        "DINGTALK_ALLOWED_USERS",
        "DINGTALK_ALLOW_ALL_USERS",
        "GATEWAY_ALLOWED_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)


def _adapter(extra=None):
    from plugins.platforms.dingtalk.adapter import DingTalkAdapter

    return DingTalkAdapter(PlatformConfig(enabled=True, extra=dict(extra or {})))


def _runner_with(adapter):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.DINGTALK: adapter.config})
    runner.adapters = {Platform.DINGTALK: adapter}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    return runner


def _source(chat_type="dm", user_id="stranger"):
    return SessionSource(
        platform=Platform.DINGTALK,
        user_id=user_id,
        chat_id="cidChat==",
        user_name="同事",
        chat_type=chat_type,
    )


# -- Access ---------------------------------------------------------------


@pytest.mark.parametrize("chat_type", ["dm", "group"])
def test_no_allowlist_requires_desktop_authorization(chat_type):
    # First contact must be approved in the desktop app (pairing), not let in.
    runner = _runner_with(_adapter())
    assert runner._is_user_authorized(_source(chat_type)) is False


def test_approved_pairing_admits_sender():
    runner = _runner_with(_adapter())
    runner.pairing_store.is_approved.return_value = True
    assert runner._is_user_authorized(_source()) is True


@pytest.mark.parametrize("chat_type", ["dm", "group"])
@pytest.mark.parametrize("opt_in", [{"allow_all_users": True}, {"allow_all_users": "true"}])
def test_explicit_opt_in_admits_sender_without_pairing(chat_type, opt_in):
    runner = _runner_with(_adapter(opt_in))
    assert runner._is_user_authorized(_source(chat_type)) is True
    # Scoped to this adapter's config, never a process-wide flag.
    assert "DINGTALK_ALLOW_ALL_USERS" not in os.environ


def test_env_opt_in_admits_sender(monkeypatch):
    monkeypatch.setenv("DINGTALK_ALLOW_ALL_USERS", "true")
    runner = _runner_with(_adapter())
    assert runner._is_user_authorized(_source()) is True


def test_configured_allowlist_still_restricts(monkeypatch):
    monkeypatch.setenv("DINGTALK_ALLOWED_USERS", "boss")
    runner = _runner_with(_adapter())
    assert runner._is_user_authorized(_source(user_id="stranger")) is False
    assert runner._is_user_authorized(_source(user_id="boss")) is True


@pytest.mark.parametrize("opt_out", [{"allow_all_users": False}, {"allow_all_users": "false"}])
def test_explicit_opt_out_keeps_pairing(opt_out):
    runner = _runner_with(_adapter(opt_out))
    assert runner._is_user_authorized(_source()) is False


def test_env_opt_out_keeps_pairing(monkeypatch):
    monkeypatch.setenv("DINGTALK_ALLOW_ALL_USERS", "false")
    runner = _runner_with(_adapter())
    assert runner._is_user_authorized(_source()) is False


def test_operator_allow_from_is_not_overwritten():
    runner = _runner_with(_adapter({"allow_all_users": True, "allow_from": ["boss"], "group_allow_from": ["boss"]}))
    assert runner._is_user_authorized(_source(user_id="stranger")) is False
    assert runner._is_user_authorized(_source(user_id="boss")) is True


# -- Inbound media --------------------------------------------------------


def _http_get_returning(data, content_type="application/octet-stream"):
    resp = MagicMock(content=data, headers={"content-type": content_type})
    resp.raise_for_status = MagicMock()
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
async def test_inbound_file_is_cached_locally_with_original_name():
    adapter = _adapter()
    adapter._http_client = _http_get_returning(b"%PDF-1.4 demo")
    message = SimpleNamespace(
        message_type="file",
        extensions={"content": {"downloadCode": "https://oss.example/x", "fileName": "季度报告.pdf"}},
    )
    urls, types = await adapter._cache_inbound_media(
        message, ["https://oss.example/x"], ["application/pdf"]
    )
    assert os.path.isfile(urls[0])
    assert urls[0].endswith("季度报告.pdf")
    assert open(urls[0], "rb").read() == b"%PDF-1.4 demo"
    assert types == ["application/pdf"]


@pytest.mark.asyncio
async def test_inbound_picture_gets_real_image_mime():
    adapter = _adapter()
    adapter._http_client = _http_get_returning(_PNG, "image/png")
    message = SimpleNamespace(message_type="picture", extensions={})
    urls, types = await adapter._cache_inbound_media(
        message, ["https://oss.example/pic"], ["image"]
    )
    assert os.path.isfile(urls[0])
    assert types[0].startswith("image/")


@pytest.mark.asyncio
async def test_inbound_download_failure_keeps_url():
    adapter = _adapter()
    client = MagicMock()
    client.get = AsyncMock(side_effect=RuntimeError("boom"))
    adapter._http_client = client
    message = SimpleNamespace(message_type="file", extensions={})
    urls, types = await adapter._cache_inbound_media(
        message, ["https://oss.example/x"], ["application/pdf"]
    )
    assert urls == ["https://oss.example/x"]
    assert types == ["application/pdf"]


# -- Outbound attachments -------------------------------------------------


def _send_ready_adapter(conversation_type="1"):
    adapter = _adapter({"robot_code": "robot-1"})
    adapter._get_access_token = AsyncMock(return_value="tok")
    adapter._message_contexts["cidChat=="] = SimpleNamespace(
        conversation_id="cidChat==",
        conversation_type=conversation_type,
        sender_staff_id="staff-9",
    )
    upload = MagicMock(json=MagicMock(return_value={"errcode": 0, "media_id": "@lAmedia"}))
    sent = MagicMock(status_code=200, content=b"{}", json=MagicMock(return_value={"processQueryKey": "q1"}))
    client = MagicMock()
    client.post = AsyncMock(side_effect=[upload, sent])
    adapter._http_client = client
    return adapter, client


@pytest.mark.asyncio
async def test_send_document_dm_uploads_and_sends_sample_file(tmp_path):
    adapter, client = _send_ready_adapter("1")
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF")
    result = await adapter.send_document("cidChat==", str(path))
    assert result.success is True
    upload_call, send_call = client.post.call_args_list
    assert upload_call.kwargs["params"] == {"access_token": "tok", "type": "file"}
    assert send_call.args[0].endswith("/robot/oToMessages/batchSend")
    body = send_call.kwargs["json"]
    assert body["userIds"] == ["staff-9"]
    assert body["robotCode"] == "robot-1"
    assert body["msgKey"] == "sampleFile"
    assert json.loads(body["msgParam"]) == {
        "mediaId": "@lAmedia", "fileName": "report.pdf", "fileType": "pdf",
    }


@pytest.mark.asyncio
async def test_send_document_group_targets_conversation(tmp_path):
    adapter, client = _send_ready_adapter("2")
    path = tmp_path / "a.xlsx"
    path.write_bytes(b"x")
    result = await adapter.send_document("cidChat==", str(path))
    assert result.success is True
    send_call = client.post.call_args_list[1]
    assert send_call.args[0].endswith("/robot/groupMessages/send")
    assert send_call.kwargs["json"]["openConversationId"] == "cidChat=="


@pytest.mark.asyncio
async def test_send_document_unsupported_type_is_zipped(tmp_path):
    adapter, client = _send_ready_adapter("1")
    path = tmp_path / "notes.md"
    path.write_text("# hi", encoding="utf-8")
    captured = {}

    async def _post(url, **kw):
        if "files" in kw:
            name, data = kw["files"]["media"]
            captured["name"] = name
            zpath = tmp_path / "got.zip"
            zpath.write_bytes(data)
            with zipfile.ZipFile(zpath) as zf:
                captured["members"] = zf.namelist()
            return MagicMock(json=MagicMock(return_value={"errcode": 0, "media_id": "@m"}))
        captured["param"] = json.loads(kw["json"]["msgParam"])
        return MagicMock(status_code=200, content=b"{}", json=MagicMock(return_value={}))

    client.post = AsyncMock(side_effect=_post)
    result = await adapter.send_document("cidChat==", str(path))
    assert result.success is True
    assert captured["name"] == "notes.md.zip"
    assert captured["members"] == ["notes.md"]
    assert captured["param"]["fileType"] == "zip"


@pytest.mark.asyncio
async def test_send_image_file_uses_sample_image_msg(tmp_path):
    adapter, client = _send_ready_adapter("1")
    path = tmp_path / "chart.png"
    path.write_bytes(_PNG)
    result = await adapter.send_image_file("cidChat==", str(path))
    assert result.success is True
    upload_call, send_call = client.post.call_args_list
    assert upload_call.kwargs["params"]["type"] == "image"
    assert send_call.kwargs["json"]["msgKey"] == "sampleImageMsg"
    assert json.loads(send_call.kwargs["json"]["msgParam"]) == {"photoURL": "@lAmedia"}


@pytest.mark.asyncio
async def test_send_reports_rejected_recipient(tmp_path):
    adapter, client = _send_ready_adapter("1")
    upload = MagicMock(json=MagicMock(return_value={"errcode": 0, "media_id": "@m"}))
    sent = MagicMock(status_code=200, content=b"{}", json=MagicMock(
        return_value={"invalidStaffIdList": ["staff-9"]}))
    client.post = AsyncMock(side_effect=[upload, sent])
    path = tmp_path / "r.pdf"
    path.write_bytes(b"%PDF")
    result = await adapter.send_document("cidChat==", str(path))
    assert result.success is False
    assert "staff-9" in result.error


@pytest.mark.asyncio
async def test_send_without_inbound_context_fails_clearly(tmp_path):
    adapter, client = _send_ready_adapter("1")
    adapter._message_contexts.clear()
    path = tmp_path / "r.pdf"
    path.write_bytes(b"%PDF")
    result = await adapter.send_document("cidChat==", str(path))
    assert result.success is False
    assert "inbound message" in result.error
