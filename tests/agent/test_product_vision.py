"""公司网关上的模型能不能直接看图 —— 走真实的托管配置与一个本地 /v1/models。

背景见 agent/product_vision.py：公司网关的模型 models.dev 查不到，以前一律被当成
不能看图，DeepSeek V4.1 Flash 只能看到辅助模型写的文字描述。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

MODELS = [
    {"id": "deepseek-v4.1-flash", "object": "model", "supports_vision": True},
    {"id": "deepseek-v4-pro", "object": "model", "supports_vision": False},
    {"id": "legacy-model", "object": "model"},
]


class _Handler(BaseHTTPRequestHandler):
    seen_auth: list[str] = []

    def do_GET(self):  # noqa: N802
        _Handler.seen_auth.append(self.headers.get("Authorization", ""))
        if self.path.rstrip("/") == "/v1/models":
            body = json.dumps({"object": "list", "data": MODELS}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):  # noqa: D401
        pass


@pytest.fixture
def gateway():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Handler.seen_auth = []
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def branded(tmp_path, monkeypatch, gateway):
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text(
        "product:\n"
        "  enabled: true\n"
        "  brand_id: test-brand\n"
        "  provider_id: llano-gateway\n"
        f"  gateway_origin: {gateway}\n"
        "model:\n"
        "  provider: llano-gateway\n"
        f"  base_url: {gateway}/v1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.setenv("LLANO_GATEWAY_API_KEY", "device-key")

    from agent import model_metadata

    model_metadata._endpoint_model_metadata_cache.clear()
    model_metadata._endpoint_model_metadata_cache_time.clear()
    return gateway


def test_网关声明能看图的模型走原图(branded):
    from agent.image_routing import decide_image_input_mode

    cfg = {"model": {"provider": "llano-gateway", "base_url": f"{branded}/v1"}}

    assert decide_image_input_mode("llano-gateway", "deepseek-v4.1-flash", cfg) == "native"
    # 网关要带设备密钥才回清单
    assert "Bearer device-key" in _Handler.seen_auth


def test_运行时身份塌成_custom_也认得公司网关(branded):
    """托管 provider 不是内置的，运行时常被归成 custom，只剩 base_url 能认。"""
    from agent.image_routing import _lookup_supports_vision

    cfg = {"model": {"provider": "custom", "base_url": f"{branded}/v1"}}

    assert _lookup_supports_vision("custom", "deepseek-v4.1-flash", cfg) is True


def test_网关明确说不能看图的模型仍转文字(branded):
    from agent.image_routing import decide_image_input_mode

    cfg = {"model": {"provider": "llano-gateway", "base_url": f"{branded}/v1"}}

    assert decide_image_input_mode("llano-gateway", "deepseek-v4-pro", cfg) == "text"


def test_老网关不发字段时行为不变(branded):
    """没有 supports_vision 就交回上游逻辑，而不是当成 False。"""
    from agent.product_vision import gateway_model_supports_vision

    assert gateway_model_supports_vision("llano-gateway", "legacy-model", f"{branded}/v1") is None


def test_别的_provider_不受影响(branded):
    from agent.product_vision import gateway_model_supports_vision, is_company_gateway

    assert is_company_gateway("custom", "https://api.example.com/v1") is False
    assert gateway_model_supports_vision("custom", "deepseek-v4.1-flash", "https://api.example.com/v1") is None


def test_stock_安装完全不走这条路(monkeypatch):
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    from agent.product_vision import is_company_gateway

    assert is_company_gateway("llano-gateway", "https://gateway.example.invalid/v1") is False


def test_公司网关的工具结果截图降级为文字(branded):
    """DeepSeek 只收 user 消息里的图，tool 消息带图会 400。"""
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent.provider = "llano-gateway"
    agent.base_url = f"{branded}/v1"

    assert agent._provider_supports_vision_tool_messages() is False

    agent.provider = "openrouter"
    agent.base_url = "https://openrouter.ai/api/v1"
    assert agent._provider_supports_vision_tool_messages() is True
