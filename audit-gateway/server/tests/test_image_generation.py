from __future__ import annotations

import asyncio
import base64
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from router.v1 import images as route
from services.forwarding import UpstreamError, UpstreamTarget
from services.image_generation import ImageRequest, decode_image, generate, normalize_result
from utils.gateway_auth import Caller, require_caller


def png():
    data = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(data, format="PNG")
    return data.getvalue()


@pytest.mark.parametrize("value", ["https://example.com/image.png", "file:///etc/passwd", "data:image/png;base64,bad"])
def test_reference_images_do_not_fetch_arbitrary_urls(value):
    with pytest.raises(ValueError):
        decode_image(value, data_url=True)


def test_generation_response_is_verified_and_normalized():
    encoded = base64.b64encode(png()).decode()
    response = {"data": [{"b64_json": encoded}]}
    assert base64.b64decode(normalize_result(response)["data"][0]["b64_json"]) == png()
    with pytest.raises(UpstreamError):
        normalize_result({"data": [{"b64_json": "not-an-image"}]})


@pytest.mark.asyncio
async def test_real_http_generation_and_edit_wire_formats():
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append((self.path, self.headers["Authorization"], self.headers["Content-Type"], raw))
            payload = json.dumps({"data": [{"b64_json": base64.b64encode(png()).decode()}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *_): pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    target = UpstreamTarget(1, f"http://127.0.0.1:{server.server_port}/v1", "test-upstream-key", 10, "gpt-image-2-t", 1)
    try:
        result = await generate(target, ImageRequest(prompt="蓝色圆形"))
        assert base64.b64decode(result["data"][0]["b64_json"]) == png()
        await generate(target, ImageRequest(prompt="改成红色", reference_images=["data:image/png;base64," + base64.b64encode(png()).decode()]))
        assert [entry[0] for entry in seen] == ["/v1/images/generations", "/v1/images/edits"]
        assert seen[0][1] == "Bearer test-upstream-key"
        assert json.loads(seen[0][3])["model"] == "gpt-image-2-t"
        assert seen[1][2].startswith("multipart/form-data;")
        assert b'name="image"' in seen[1][3]
        assert png() in seen[1][3]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def app(monkeypatch):
    app = FastAPI()
    app.include_router(route.router, prefix="/v1")
    app.dependency_overrides[require_caller] = lambda: Caller(
        uid="image-user", username="测试用户", role="employee", dept_id=1, dept_code="test", auth_type="apikey", api_key_id=5)
    facts = []
    monkeypatch.setattr(route, "_record_in_background", facts.append)
    app.state.facts = facts
    return app


def test_unauthorized_image_never_reaches_upstream(app, monkeypatch):
    async def deny(*_): return None
    monkeypatch.setattr(route, "check_model_permission", deny)
    response = TestClient(app).post("/v1/images/generations", json={"model": "gpt-image-2", "prompt": "圆形"})
    assert response.status_code == 403
    assert app.state.facts == []


def test_unknown_sensitive_fields_are_rejected_before_routing(app):
    response = TestClient(app).post("/v1/images/generations", json={"prompt": "圆形", "api_key": "client-supplied", "base_url": "http://localhost"})
    assert response.status_code == 400


def test_group_credential_cannot_generate_images(app):
    app.dependency_overrides[require_caller] = lambda: Caller(
        uid="group-user", username="测试", role="employee", dept_id=1, dept_code="test", auth_type="apikey", api_key_id=6, scope="group")
    response = TestClient(app).post("/v1/images/generations", json={"prompt": "圆形"})
    assert response.status_code == 403


def wire_authorized(monkeypatch, generator):
    from model.database import ModelInfo
    model = ModelInfo(id=1, provider_id=2, model_name="gpt-image-2-t", alias="image-token", display_name="生图", billing_mode="token", currency="CNY")
    async def allow(*_): return model
    async def quotas(*_): return []
    async def provider(*_): return SimpleNamespace(id=2)
    async def keywords(*_, **__): return []
    monkeypatch.setattr(route, "check_model_permission", allow)
    monkeypatch.setattr(route, "applicable_quota_statuses", quotas)
    monkeypatch.setattr(route, "get_provider", provider)
    monkeypatch.setattr(route, "keywords_for", keywords)
    monkeypatch.setattr(route, "build_target", lambda *_: "trusted-target")
    monkeypatch.setattr(route, "generate", generator)


def test_generation_records_raw_usage_and_both_reference_and_output_attachments(app, monkeypatch):
    async def generated(target, body):
        assert target == "trusted-target"
        return {"data": [{"b64_json": base64.b64encode(png()).decode(), "mime_type": "image/png"}],
                "usage": {"input_tokens": 11, "output_tokens": 27, "total_tokens": 38}}
    wire_authorized(monkeypatch, generated)
    response = TestClient(app).post("/v1/images/generations", json={"model": "image-token", "prompt": "蓝色圆形", "reference_images": ["data:image/png;base64," + base64.b64encode(png()).decode()]})
    assert response.status_code == 200
    facts = app.state.facts[0]
    assert facts.status == "success" and facts.call_kind == "image"
    assert (facts.prompt_tokens, facts.completion_tokens, facts.total_tokens) == (11, 27, 38)
    assert facts.usage_raw == {"input_tokens": 11, "output_tokens": 27, "total_tokens": 38}
    assert facts.tokens_estimated is False
    from services.attachments import extract_attachments
    _, attachments = extract_attachments(facts.request_messages)
    assert len(attachments) == 2


def test_upstream_failure_is_recorded_and_not_reported_as_success(app, monkeypatch):
    async def failed(*_): raise UpstreamError("生图超时", 504, "image_timeout")
    wire_authorized(monkeypatch, failed)
    response = TestClient(app).post("/v1/images/generations", json={"model": "image-token", "prompt": "圆形"})
    assert response.status_code == 504
    assert app.state.facts[0].status == "failed"
    assert app.state.facts[0].error_code == "image_timeout"


@pytest.mark.asyncio
async def test_cancellation_still_schedules_accounting(app, monkeypatch):
    async def cancelled(*_): raise asyncio.CancelledError()
    wire_authorized(monkeypatch, cancelled)
    request = SimpleNamespace(headers={}, state=SimpleNamespace(request_id="cancelled"))
    async def body(): return {"model": "image-token", "prompt": "圆形"}
    request.json = body
    caller = app.dependency_overrides[require_caller]()
    with pytest.raises(asyncio.CancelledError):
        await route.image_generate(request, caller)
    assert app.state.facts[0].aborted is True
    assert app.state.facts[0].error_code == "image_cancelled"


def test_department_redaction_is_applied_before_the_image_provider(app, monkeypatch):
    seen = []
    async def generated(target, body):
        seen.append(body.prompt)
        return {"data": [{"b64_json": base64.b64encode(png()).decode(), "mime_type": "image/png"}]}
    wire_authorized(monkeypatch, generated)
    async def keywords(dept_id, *, strict=False):
        assert dept_id == 1 and strict is True
        return ["部门秘密"]
    monkeypatch.setattr(route, "keywords_for", keywords)
    response = TestClient(app).post('/v1/images/generations', json={'model': 'image-token', 'prompt': '绘制部门秘密海报'})
    assert response.status_code == 200
    assert seen == ['绘制[公司]海报']
    assert app.state.facts[0].masked is True


def test_unavailable_redaction_policy_causes_zero_image_provider_calls(app, monkeypatch):
    calls = []
    async def generated(*_): calls.append(True)
    wire_authorized(monkeypatch, generated)
    async def unavailable(*_, **__): raise RuntimeError('policy unavailable')
    monkeypatch.setattr(route, 'keywords_for', unavailable)
    response = TestClient(app).post('/v1/images/generations', json={'model': 'image-token', 'prompt': '绘制海报'})
    assert response.status_code == 503
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("model,protocol", [
    ("gemini-3.1-flash-image-preview", "gemini"),
    ("doubao-seedream-5-0-260128", "seedream"),
])
async def test_new_models_real_http_reference_protocols(model, protocol):
    seen = []
    encoded = base64.b64encode(png()).decode()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, body))
            output = ({"choices": [{"message": {"content": "![图](data:image/png;base64," + encoded + ")"}}]}
                      if protocol == "gemini" else {"data": [{"b64_json": encoded}]})
            output["usage"] = {"output_tokens": 17, "total_tokens": 17}
            raw = json.dumps(output).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def log_message(self, *_): pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    ref = "data:image/png;base64," + encoded
    try:
        target = UpstreamTarget(1, f"http://127.0.0.1:{server.server_port}/v1", "test", 10, model, 1)
        result = await generate(target, ImageRequest(prompt="蓝色圆形", size="1536x1024", reference_images=[ref]))
        assert result["data"][0]["b64_json"] == encoded
        assert result["usage"]["output_tokens"] == 17
        path, body = seen[0]
        assert body["model"] == model
        if protocol == "gemini":
            assert path == "/v1/chat/completions"
            assert body["messages"][0]["content"][1]["image_url"]["url"] == ref
            assert body["extra_body"]["google"]["image_config"]["aspect_ratio"] == "3:2"
        else:
            assert path == "/v1/images/generations"
            assert body["image"] == [ref]
            assert body["size"] == "2496x1664"
            assert body["response_format"] == "b64_json"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("content", ["https://example.com/image.png", "no image", []])
def test_gemini_missing_image_does_not_fetch_remote_urls(content):
    with pytest.raises(UpstreamError):
        normalize_result({"choices": [{"message": {"content": content}}]}, protocol="gemini")


def test_output_only_seedream_usage_records_actual_completion_tokens(app, monkeypatch):
    async def generated(*_):
        return {"data": [{"b64_json": base64.b64encode(png()).decode(), "mime_type": "image/png"}],
                "usage": {"generated_images": 1, "output_tokens": 16384, "total_tokens": 16384}}
    wire_authorized(monkeypatch, generated)
    response = TestClient(app).post("/v1/images/generations", json={"model": "image-token", "prompt": "圆形"})
    assert response.status_code == 200
    facts = app.state.facts[0]
    assert (facts.prompt_tokens, facts.completion_tokens, facts.total_tokens) == (0, 16384, 16384)
    assert facts.usage_raw == response.json()["usage"]
    assert facts.tokens_estimated is False
