"""Reject oversized uploads before JSON/multipart parsing or model side effects."""
from starlette.responses import JSONResponse


class RequestBodyLimitMiddleware:
    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)

        async def reject():
            response = JSONResponse(status_code=413, content={
                'code': 10006,
                'message': '请求内容过大，请减少附件或拆分发送',
                'error': {'type': 'invalid_request_error', 'code': 'request_body_too_large',
                          'message': '请求内容过大，请减少附件或拆分发送'},
            })
            await response(scope, receive, send)

        for key, value in scope.get('headers', []):
            if key.lower() == b'content-length':
                try:
                    if int(value) > self.max_bytes:
                        return await reject()
                except ValueError:
                    pass  # Actual bytes below remain authoritative.

        # Bound the body before invoking handlers. This also covers chunked or
        # missing/false Content-Length uploads; handlers cannot catch a size
        # exception and accidentally continue to the upstream model.
        chunks = []
        size = 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            body = message.get('body', b'')
            size += len(body)
            if size > self.max_bytes:
                return await reject()
            if body:
                chunks.append(body)
            if not message.get('more_body', False):
                break

        # Release each buffered chunk as the downstream consumes it. Response
        # streaming and disconnect detection still use the original channel.
        from collections import deque
        pending = deque(chunks)
        del chunks
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                body = pending.popleft() if pending else b''
                delivered = not pending
                return {'type': 'http.request', 'body': body, 'more_body': not delivered}
            return await receive()

        await self.app(scope, replay, send)
