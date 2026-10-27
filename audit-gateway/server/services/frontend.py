"""管理后台前端的托管 —— 让网关自己发静态文件，不再多一层 nginx

## 为什么要判断「这个路径该不该回 index.html」

后台用的是 vue-router 的 history 模式（`createWebHistory`）。地址栏里的
`/audit/list` 是前端自己的路由，服务端并没有这个文件。用户在这个页面上按 F5，
请求会实打实打到网关 —— 不回 index.html 就是 404，表现为「刷新一下就白屏」，
而第一次进入时一切正常。

所以要有兜底。但兜底不能无差别：

### API 路径不兜底

`/admin/typo` 这种打错的接口必须回 404 JSON，不能回 index.html。否则前端拿到
一坨 HTML 去 `JSON.parse`，报的是「Unexpected token <」—— 那句话既不告诉你路径
错了，也不告诉你错在哪一段。调接口时这个假象能耗掉半小时。

### 静态资源不兜底

`/assets/index-abc123.js` 要是没找到就该 404。回 index.html 的话浏览器会拿到
`Content-Type: text/html` 的「JavaScript」，控制台报的是 MIME 类型错误，
而真正的原因是那个文件根本不在包里（前端没构建、或者构建产物过期）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# 归后端所有的路径前缀。这些下面的一切都不走 SPA 兜底。
API_PREFIXES = ("/admin", "/client", "/v1", "/static", "/healthz", "/readyz", "/docs", "/openapi.json")

# 一看就是静态资源的后缀。找不到就 404，别回 HTML。
ASSET_SUFFIXES = (
    ".js", ".mjs", ".css", ".map", ".json", ".ico", ".png", ".jpg", ".jpeg",
    ".gif", ".svg", ".webp", ".woff", ".woff2", ".ttf", ".eot", ".wasm", ".txt",
)


def is_api_path(path: str) -> bool:
    normalized = "/" + str(path or "").lstrip("/")
    return any(
        normalized == prefix or normalized.startswith(f"{prefix}/")
        for prefix in API_PREFIXES
    )


def looks_like_asset(path: str) -> bool:
    return str(path or "").lower().endswith(ASSET_SUFFIXES)


def should_serve_spa(path: str) -> bool:
    """这个路径该不该回 index.html。"""
    return not is_api_path(path) and not looks_like_asset(path)


def resolve_frontend_dist(configured: Optional[str], server_dir: Path) -> Optional[Path]:
    """定位前端构建产物目录，没有就返回 None（开发期走 vite dev server）。

    默认往 `../client/dist` 找 —— 仓库里前端和后端是并排的两个目录。
    """
    if configured:
        candidate = Path(configured).expanduser()
    else:
        candidate = server_dir.parent / "client" / "dist"

    if not candidate.is_dir() or not (candidate / "index.html").is_file():
        return None
    return candidate
