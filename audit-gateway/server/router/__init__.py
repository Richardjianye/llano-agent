"""路由注册

/admin/*   管理端（账号、部门、模型、白名单、API Key）
/client/*  桌面客户端辅助接口
/v1/*      OpenAI 兼容的转发接口（审计与用量的唯一写入点）
/healthz /readyz  运维探针
"""

from . import admin, client, health, v1

__all__ = ["admin", "client", "health", "v1"]
