"""中间件与全局异常处理

- RequestContextMiddleware: 生成 request_id，记访问日志，兜底未捕获异常
- register_exception_handlers: 把各类异常统一成 Response 结构

request_id 会写入 request.state 并回写响应头 X-Request-Id，
后续会话审计、凭证访问日志都用它串联同一次请求。
"""

import time
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger
from model.response import Response as ApiResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

REQUEST_ID_HEADER = "X-Request-Id"

# 访问日志跳过的高频探针路径
_QUIET_PATHS = {"/healthz", "/readyz", "/favicon.ico"}


def get_request_id(request: Request) -> str:
    """取当前请求ID，供审计日志使用"""
    return getattr(request.state, "request_id", "")


def get_client_ip(request: Request) -> str:
    """取客户端真实 IP（考虑反向代理）"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


class RequestContextMiddleware(BaseHTTPMiddleware):
    """请求上下文：request_id + 耗时日志"""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        request.state.client_ip = get_client_ip(request)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            cost_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                f"未捕获异常 rid={request_id} {request.method} {request.url.path} "
                f"cost={cost_ms:.1f}ms err={exc}"
            )
            return JSONResponse(
                status_code=500,
                content=ApiResponse(code=-1, message="服务器内部错误").model_dump(),
                headers={REQUEST_ID_HEADER: request_id},
            )

        cost_ms = (time.perf_counter() - started) * 1000
        if request.url.path not in _QUIET_PATHS:
            logger.info(
                f"rid={request_id} {request.method} {request.url.path} "
                f"-> {response.status_code} {cost_ms:.1f}ms"
            )
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器"""
    from utils.errors import ApiKeyAuthError, AppError

    @app.exception_handler(ApiKeyAuthError)
    async def _api_key_auth_error_handler(request: Request, exc: ApiKeyAuthError):
        logger.warning(
            f"API Key 认证失败 rid={get_request_id(request)} path={request.url.path} "
            f"code={exc.code} msg={exc.message}"
        )
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "message": exc.message,
                    "type": "gateway_auth_error",
                    "code": "gateway_auth_failed",
                }
            },
        )

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError):
        logger.warning(
            f"业务异常 rid={get_request_id(request)} path={request.url.path} "
            f"code={exc.code} msg={exc.message}"
        )
        return JSONResponse(
            status_code=exc.http_status,
            content=ApiResponse(
                code=exc.code, message=exc.message, data=exc.data
            ).model_dump(),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(p) for p in first.get("loc", [])[1:]) or "请求体"
        message = f"{field}: {first.get('msg', '参数不合法')}"
        logger.warning(
            f"参数校验失败 rid={get_request_id(request)} path={request.url.path} {message}"
        )
        return JSONResponse(
            status_code=200,
            content=ApiResponse(code=10006, message=message).model_dump(),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(request: Request, exc: StarletteHTTPException):
        # 404/405 保留原状态码，便于运维排查；响应体仍是统一结构
        return JSONResponse(
            status_code=exc.status_code,
            content=ApiResponse(code=-1, message=str(exc.detail)).model_dump(),
        )
