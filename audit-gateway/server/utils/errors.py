"""统一业务异常

模板原有写法是在路由里 `return Response(code=xxx)`，依赖项里没法这样短路
（返回值只会变成参数，不会中断请求）。因此依赖项与服务层统一抛 AppError，
由 main.py 注册的处理器转成同样的 Response 结构，前端无感知。
"""

from typing import Any, Optional

from model.response import code_map


class AppError(Exception):
    """业务异常 —— 携带统一错误码，HTTP 状态码默认 200 以保持既有响应约定"""

    def __init__(
        self,
        code: int,
        message: Optional[str] = None,
        data: Any = None,
        http_status: int = 200,
    ):
        self.code = code
        self.message = message or code_map.get(code, code_map[-1])
        self.data = data if data is not None else {}
        self.http_status = http_status
        super().__init__(self.message)


class AuthError(AppError):
    """未认证 / 登录态失效"""

    def __init__(self, message: Optional[str] = None, code: int = 10002):
        # OpenAI-compatible /v1 clients key their reauthentication behavior off
        # HTTP 401. Keeping auth failures at HTTP 200 made revoked desktop keys
        # look like malformed successful model responses and encouraged retry
        # loops inside SDKs.
        super().__init__(code=code, message=message, http_status=401)


class ApiKeyAuthError(AuthError):
    """OpenAI-compatible API Key rejection.

    The `/v1` client must receive an OpenAI error shape rather than the admin
    UI's `{code,message,data}` envelope, while retaining HTTP 401 semantics.
    """


class PermissionError_(AppError):
    """已认证但无权限"""

    def __init__(self, message: Optional[str] = None):
        super().__init__(code=10008, message=message)


class NotFoundError(AppError):
    """目标资源不存在"""

    def __init__(self, message: str = "目标不存在"):
        super().__init__(code=10009, message=message)


class ConflictError(AppError):
    """唯一性冲突，如名称/编码已存在"""

    def __init__(self, message: Optional[str] = None):
        super().__init__(code=10005, message=message)


class ValidationError_(AppError):
    """入参不合法"""

    def __init__(self, message: str = "参数不合法"):
        super().__init__(code=10006, message=message)


class McpUpstreamError(AppError):
    """MCP 上游失败；仅携带可安全返回给调用方的分类与消息。"""

    def __init__(
        self, error_code: str, message: str, http_status: int = 502
    ) -> None:
        self.error_code = error_code
        super().__init__(code=10010, message=message, http_status=http_status)
