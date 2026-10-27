"""Management contracts: no paid discovery, field-level Codex protection, shared accounting."""
import base64
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from PIL import Image
from fastapi import FastAPI
from fastapi.testclient import TestClient
from model.request.org import ProviderCreate, ProviderUpdate
from router.admin import catalog, images as admin_images
from router.v1 import images
from services.image_catalog import discover
from utils.errors import ValidationError_
from utils.middleware import register_exception_handlers


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [{'api_key': 'ordinary-key'}, {'base_url': 'https://example.com/v1'}])
async def test_codex_sensitive_fields_rejected_before_write(monkeypatch, changes):
    async def provider(_): return SimpleNamespace(base_url='codex://chatgpt')
    async def write(*_, **__): pytest.fail('must not write')
    monkeypatch.setattr(catalog, 'get_provider', provider)
    monkeypatch.setattr(catalog, 'update_provider', write)
    with pytest.raises(ValidationError_):
        await catalog.provider_update(1, ProviderUpdate(**changes), SimpleNamespace(uid='admin'))


@pytest.mark.asyncio
async def test_codex_metadata_update_preserves_credential(monkeypatch):
    calls = []
    async def provider(_): return SimpleNamespace(base_url='codex://chatgpt')
    async def write(*_, **kwargs): calls.append(kwargs)
    monkeypatch.setattr(catalog, 'get_provider', provider)
    monkeypatch.setattr(catalog, 'update_provider', write)
    await catalog.provider_update(1, ProviderUpdate(name='改名', status=0, timeout_seconds=90), SimpleNamespace(uid='admin'))
    assert calls[0]['name'] == '改名' and calls[0]['status'] == 0
    assert calls[0]['api_key'] is None and calls[0]['base_url'] is None


@pytest.mark.asyncio
async def test_generic_create_cannot_forge_codex_credentials():
    with pytest.raises(ValidationError_):
        await catalog.provider_create(ProviderCreate(code='invalid', name='测试', base_url='codex://chatgpt', api_key='ordinary'), SimpleNamespace(uid='admin'))


@pytest.mark.asyncio
async def test_discovery_real_http_never_generates_or_returns_key(monkeypatch):
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers['Authorization']))
            body = json.dumps({'data': [{'id': 'gpt-image-2'}, {'id': 'unknown-image'}, {'id': 'gpt-image-2'}]}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr('services.image_catalog.decrypt_secret', lambda *_: 'synthetic-key')
    try:
        result = await discover(SimpleNamespace(status=1, api_key_encrypted='encrypted', base_url=f'http://127.0.0.1:{server.server_port}/v1', timeout_seconds=10))
        assert seen == [('/v1/models', 'Bearer synthetic-key')]
        assert len(result) == 2
        assert next(m for m in result if m['id'] == 'gpt-image-2')['supported']
        assert not next(m for m in result if m['id'] == 'unknown-image')['supported']
        assert 'synthetic-key' not in json.dumps(result)
    finally:
        server.shutdown(); server.server_close(); thread.join()


@pytest.fixture
def management_app():
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(admin_images.router, prefix='/admin')
    operator = SimpleNamespace(uid='administrator', username='测试管理员', permission='president', dept_id=1, dept_code='exec')
    for route in admin_images.router.routes:
        for dep in route.dependant.dependencies:
            app.dependency_overrides[dep.call] = lambda: operator
    return app


def test_admin_request_cannot_choose_raw_provider_or_key(management_app):
    response = TestClient(management_app).post('/admin/images/test', json={'model_id': 1, 'prompt': 'test', 'api_key': 'raw', 'base_url': 'http://localhost'})
    assert response.json()['code'] != 0


def test_admin_test_reuses_quota_and_records_real_operator(management_app, monkeypatch):
    from model.database import ModelInfo
    model = ModelInfo(id=1, provider_id=2, model_name='gpt-image-2', display_name='图像', status=1, billing_mode='per_call', currency='CNY')
    async def get_model(_): return model
    async def get_provider(_): return SimpleNamespace(id=2)
    async def keywords(*_, **__): return []
    async def quotas(uid, dept, mid):
        assert (uid, dept, mid) == ('administrator', 1, 1)
        return []
    raw = io.BytesIO(); Image.new('RGB', (8, 8), 'blue').save(raw, format='PNG')
    async def generated(*_):
        return {'data': [{'b64_json': base64.b64encode(raw.getvalue()).decode(), 'mime_type': 'image/png'}], 'usage': {'output_tokens': 20, 'total_tokens': 20}}
    facts = []
    monkeypatch.setattr(admin_images, 'get_model', get_model)
    monkeypatch.setattr(images, 'get_provider', get_provider)
    monkeypatch.setattr(images, 'keywords_for', keywords)
    monkeypatch.setattr(images, 'applicable_quota_statuses', quotas)
    monkeypatch.setattr(images, 'build_target', lambda *_: None)
    monkeypatch.setattr(images, 'generate', generated)
    monkeypatch.setattr(images, '_record_in_background', facts.append)
    response = TestClient(management_app).post('/admin/images/test', json={'model_id': 1, 'prompt': '蓝色圆形'})
    assert response.json()['code'] == 0
    assert facts[0].user_uid == 'administrator' and facts[0].call_kind == 'image'
    assert facts[0].completion_tokens == 20 and facts[0].status == 'success'


@pytest.mark.asyncio
async def test_codex_list_has_stable_catalog_identity_without_upstream_calls(monkeypatch):
    from router.admin import codex
    from model.response import Response
    async def providers(): return [SimpleNamespace(id=31, base_url='codex://chatgpt', api_key_encrypted='encrypted', timeout_seconds=120, remark=None)]
    async def models(): return [(SimpleNamespace(provider_id=31), 'code')]
    async def status(pid, operator):
        return Response(data={'provider_id': pid, 'code': 'code', 'name': '测试', 'credential_ok': True})
    monkeypatch.setattr('db.selects.catalog.list_providers', providers)
    monkeypatch.setattr('db.selects.catalog.list_models', models)
    monkeypatch.setattr(codex, 'provider_status', status)
    data = (await codex.providers(SimpleNamespace(uid='admin'))).data['items'][0]
    assert data['id'] == data['provider_id'] == 31
    assert data['model_count'] == 1
    assert 'api_key_encrypted' not in data


def test_admin_test_obeys_same_quota_before_upstream(management_app, monkeypatch):
    from model.database import ModelInfo
    from services.quota import QuotaStatus
    async def get_model(_): return ModelInfo(id=1, provider_id=2, model_name='gpt-image-2', display_name='图像', status=1)
    async def exhausted(*_): return [QuotaStatus(1, 'user', 'administrator', 1, 1)]
    async def paid(*_): pytest.fail('quota must block before upstream')
    monkeypatch.setattr(admin_images, 'get_model', get_model)
    monkeypatch.setattr(images, 'applicable_quota_statuses', exhausted)
    monkeypatch.setattr(images, 'generate', paid)
    response = TestClient(management_app).post('/admin/images/test', json={'model_id': 1, 'prompt': '蓝色圆形'})
    assert response.json()['code'] != 0
    assert '额度' in response.json()['message']
