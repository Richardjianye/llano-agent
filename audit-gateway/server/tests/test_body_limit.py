import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from utils.body_limit import RequestBodyLimitMiddleware


@pytest.mark.asyncio
@pytest.mark.parametrize('length', [None, b'1', b'100'])
async def test_oversized_body_never_reaches_handler(length):
    called = False
    app = FastAPI()

    @app.post('/')
    async def handle(request: Request):
        nonlocal called
        called = True
        return await request.json()

    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=8)
    frames = iter([
        {'type': 'http.request', 'body': b'12345', 'more_body': True},
        {'type': 'http.request', 'body': b'67890', 'more_body': False},
    ])
    async def receive(): return next(frames)
    sent = []
    async def send(message): sent.append(message)
    await app({'type': 'http', 'method': 'POST', 'path': '/', 'headers': [] if length is None else [(b'content-length', length)]}, receive, send)
    assert not called
    assert sent[0]['status'] == 413
    assert b'request_body_too_large' in sent[1]['body']


@pytest.mark.asyncio
async def test_exact_limit_preserves_chunked_input_and_streamed_response():
    app = FastAPI()

    @app.post('/')
    async def handle(request: Request):
        assert await request.body() == b'12345678'
        async def output():
            yield b'first'
            yield b'second'
        return StreamingResponse(output())

    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=8)
    frames = iter([
        {'type': 'http.request', 'body': b'1234', 'more_body': True},
        {'type': 'http.request', 'body': b'5678', 'more_body': False},
    ])
    async def receive(): return next(frames)
    sent = []
    async def send(message): sent.append(message)
    await app({'type': 'http', 'asgi': {'spec_version': '2.4'}, 'method': 'POST', 'path': '/', 'headers': [], 'query_string': b''}, receive, send)
    assert sent[0]['status'] == 200
    assert [x['body'] for x in sent[1:]] == [b'first', b'second', b'']


@pytest.mark.asyncio
async def test_disconnected_upload_does_not_start_handler():
    async def app(*args): pytest.fail('disconnected upload reached handler')
    async def receive(): return {'type': 'http.disconnect'}
    async def send(message): pytest.fail('response sent to disconnected upload')
    await RequestBodyLimitMiddleware(app, 8)({'type': 'http', 'headers': []}, receive, send)
