from __future__ import annotations

import base64
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image

from tools import llano_image_generation as images


def png():
    data = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(data, format="PNG")
    return data.getvalue()


def managed(monkeypatch, tmp_path, origin, *, enabled=True):
    directory = tmp_path / "managed"
    directory.mkdir()
    (directory / "config.yaml").write_text(
        "product:\n  enabled: true\n  brand_id: llano-ai-workbench\n  provider_id: llano-gateway\n"
        f"  gateway_origin: {origin}\n  managed_image_generation: {str(enabled).lower()}\n"
        "  disabled_capabilities: [builtin_image_generation]\n")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(directory))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLANO_GATEWAY_API_KEY", "synthetic-device-key")


def test_managed_images_require_explicit_build_policy_and_do_not_unblock_builtin(monkeypatch, tmp_path):
    managed(monkeypatch, tmp_path, "http://127.0.0.1", enabled=False)
    assert images.available() is False
    from hermes_cli.product_policy import capability_allowed
    assert capability_allowed("builtin_image_generation") is False


@pytest.mark.parametrize("model", ["gpt-image-2-t", "gemini-3.1-flash-image-preview", "doubao-seedream-5-0-260128"])
def test_real_http_device_auth_to_local_gateway_saves_image_without_exposing_base64(monkeypatch, tmp_path, model):
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            raw = json.dumps({"data": [{"id": model}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_POST(self):
            seen.append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            raw = json.dumps({"model": model, "data": [{"b64_json": base64.b64encode(png()).decode()}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def log_message(self, *_): pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        managed(monkeypatch, tmp_path, f"http://127.0.0.1:{server.server_port}")
        assert images.available() is True
        from tools.image_generation_tool import _handle_image_generate, check_image_generation_requirements
        assert check_image_generation_requirements() is True
        result = json.loads(_handle_image_generate({"prompt": "蓝色圆形", "aspect_ratio": "square", "model": model}))
        assert result["success"] is True
        path = Path(result["image"])
        assert path.is_relative_to(tmp_path / "home" / "cache" / "images")
        assert path.read_bytes() == png()
        assert "b64_json" not in result
        assert seen == [("/v1/images/generations", "Bearer synthetic-device-key", {
            "prompt": "蓝色圆形", "size": "1024x1024", "reference_images": [], "model": model})]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_remote_reference_url_is_not_downloaded_by_managed_image_client(monkeypatch, tmp_path):
    managed(monkeypatch, tmp_path, "http://127.0.0.1:1")
    result = json.loads(images.generate_image("圆形", "square", image_url="http://169.254.169.254/"))
    assert result["success"] is False
    assert "参考图" in result["error"]


def test_bad_managed_policy_cannot_fall_through_to_direct_credentials(monkeypatch, tmp_path):
    directory = tmp_path / "managed"
    directory.mkdir()
    (directory / "config.yaml").write_text("not: [valid")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(directory))
    monkeypatch.setenv("FAL_KEY", "must-not-be-read")
    from tools.image_generation_tool import _handle_image_generate, check_image_generation_requirements
    assert check_image_generation_requirements() is False
    assert "capability_unavailable" in _handle_image_generate({"prompt": "圆形"})
