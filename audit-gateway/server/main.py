"""FastAPI 应用入口"""

import os
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import uvicorn
from config import get_settings
from db import close_db_engine, init_db, init_db_engine
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from router import admin, client, health, open_api, v1

from services.frontend import resolve_frontend_dist, should_serve_spa
from utils.cache import CacheManager
from utils.errors import NotFoundError
from utils.middleware import (RequestContextMiddleware,
                              register_exception_handlers)

# ==================== 日志配置 ====================


# prod 下接管 loguru 的输出格式。与开发期默认格式保持一致，便于对着查。
_PROD_LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
    "{name}:{function}:{line} - {message}"
)


def setup_loguru(environment: str | None = None):
    """配置日志系统

    RequestContextMiddleware 已经打了带 request_id 和耗时的访问日志，
    uvicorn 自带的 access 日志是同一件事的第二遍，这里关掉避免每个请求刷两行。

    ⚠️ prod 下必须保证「至少还有一个 sink」

    这里曾经是个真实事故：prod 分支先 `logger.remove()` 掐掉默认 sink，再**只在
    gunicorn.error 有 handler 时**才 add 回来。用 `uvicorn main:app` 起服务时
    gunicorn 根本不在，那个 if 不成立，于是**全部 loguru 日志静默丢弃** ——
    没有访问日志、没有启动自检、也没有那条只打印一次的总裁初始密码。

    丢日志本身就够糟，更糟的是它看上去像「服务很安静」：uvicorn 自己的日志照常
    刷，端口照常监听，请求照常返回。

    所以现在的顺序反过来：先确保有 sink，再看有没有更好的去处。
    """
    import logging
    import sys

    # 关掉 uvicorn 的 access logger（另有 uvicorn.run(access_log=False) 兜底，
    # 用 gunicorn 起时只有这里生效）
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = False
    access_logger.disabled = True

    # 用 get_settings() 而不是 os.getenv：ENVIRONMENT 常常只写在 .env.prod 里，
    # 而 os.getenv 看不见 .env 文件。两处口径不一致的话，会出现「应用以为自己在
    # prod、日志配置以为自己在 dev」这种各说各话的状态。
    env = environment if environment is not None else get_settings().environment
    if env != "prod":
        return

    logger.remove()

    gunicorn_logger = logging.getLogger("gunicorn.error")
    if gunicorn_logger.handlers:
        logger.add(
            gunicorn_logger.handlers[0],
            level=gunicorn_logger.level,
            enqueue=True,
            backtrace=False,
            diagnose=False,
        )
        return

    # 不是 gunicorn 起的（uvicorn 直起、docker CMD、systemd…）。落回 stderr，
    # 而不是什么都不做。
    logger.add(
        sys.stderr,
        level=get_settings().log_level or "INFO",
        format=_PROD_LOG_FORMAT,
        backtrace=False,
        diagnose=False,
    )


setup_loguru()

# ==================== 全局配置 ====================

config = get_settings()


def check_startup_config(settings) -> None:
    """启动自检 —— 生产环境缺关键配置直接拒绝启动，别等出事才发现"""
    problems = []

    if settings.jwt_secret_key == "secret" or settings.jwt_refresh_secret_key == "secret":
        problems.append("JWT_SECRET_KEY / JWT_REFRESH_SECRET_KEY 仍为默认值")
    if not settings.credential_encryption_key:
        problems.append("未配置 CREDENTIAL_ENCRYPTION_KEY（中台凭证无法加密存储）")
    if not settings.provider_encryption_key:
        problems.append("未配置 PROVIDER_ENCRYPTION_KEY（上游 API Key 无法加密存储）")
    # 通讯录同步可以先上线；只有登录入口需要正式回调地址。
    directory_fields = (
        settings.dingtalk_client_id,
        settings.dingtalk_client_secret,
        settings.dingtalk_corp_id,
    )
    if any(directory_fields) and not all(directory_fields):
        problems.append("钉钉通讯录配置不完整（Client ID / Secret / Corp ID）")
    if settings.dingtalk_redirect_uri:
        from services.dingtalk_identity import configured
        if not configured(settings):
            problems.append("钉钉网页登录须配置完整凭据及 HTTPS /admin/user/dingtalk/callback")

    if not problems:
        return

    if settings.is_prod:
        raise RuntimeError("生产环境配置不完整：" + "；".join(problems))
    for p in problems:
        logger.warning(f"配置提醒（dev 放行，prod 会拒绝启动）：{p}")


def assert_timezone() -> None:
    """启动自检 —— 容器时区必须是东八区

    `model/database.now()` 存的是 naive 的**服务器本地时间**，整个库、所有报表
    的日切都建立在「本地时间 = 东八区」这个前提上。而这个前提在 2026-09-03 之前
    没有任何一处在保证：镜像没设 TZ、部署平台也没配，容器一直跑在 UTC，于是
    后台每一处时间都比实际早 8 小时。

    **这类偏差不会报错**，只会让所有人觉得「时间怎么不对」，而排查方向通常先
    奔着前端格式化去。所以这里在启动时把它喊出来一次。

    只告警不拒绝启动：时区不对是数据口径问题，不是安全问题，把服务拦在门外
    换不来任何好处 —— 而告警至少会出现在容器日志的第一屏。
    """
    from datetime import datetime

    offset = datetime.now().astimezone().utcoffset()
    expected = timedelta(hours=8)
    if offset == expected:
        return

    logger.warning(
        f"⚠️ 容器时区不是东八区（当前 UTC{offset}），"
        f"而 now() 存的是本地时间：库里的时间戳会整体偏移。"
        f"请设 TZ=Asia/Shanghai（镜像里已 ENV，部署平台覆盖了才会出现这条）"
    )


async def warn_schema_drift() -> None:
    """启动时检查「模型加了列但迁移没跑」—— 这是本项目最容易踩的坑

    `create_all` 只建缺失的表，不给已有表加列。漏跑 migrations 时服务照常启动，
    直到第一次转发写用量才炸 `Unknown column`，而那时用户看到的是「对话失败」，
    离真正的原因隔了好几层。所以宁可在启动时吵一次。

    dev 只告警不拦（本地常有半迁移状态），prod 直接拒绝启动 ——
    带着缺列上线，等于让所有写入在运行时随机失败。
    """
    from db import check_schema_drift

    try:
        missing = await check_schema_drift()
    except Exception as exc:
        # 自检本身不该阻断启动（比如权限不足读不到 information_schema）
        logger.warning(f"表结构自检未能完成，已跳过：{exc}")
        return

    if not missing:
        return

    hint = (
        f"数据库缺少 {len(missing)} 个列：{'、'.join(missing)}。"
        f"请执行 scripts/migrations/ 下对应的 SQL（create_all 不会给已有表加列）"
    )
    if get_settings().is_prod:
        raise RuntimeError(hint)
    logger.warning(f"表结构落后于代码（dev 放行，prod 会拒绝启动）：{hint}")


# ==================== 应用工厂 ====================


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # ---- 启动 ----
    check_startup_config(get_settings())
    assert_timezone()
    await init_db_engine()
    await init_db()
    await warn_schema_drift()

    cache_manager = CacheManager.get_instance()
    await cache_manager.prewarm_pool()

    from services.project_repository import schedule_pending_repositories
    pending_repositories = await schedule_pending_repositories()
    if pending_repositories:
        logger.info(f"已调度 {pending_repositories} 个待恢复的项目仓库")

    # Codex 渠道掉号巡检：空闲渠道也能被发现掉线（间隔 0 可关）
    from services.codex_channel import health as codex_health

    codex_health.start_watchdog(get_settings().codex_health_interval_seconds)

    # 闲置超过阈值（默认 30 天）的设备 Key 定时吊销
    from services import apikey_lifecycle

    apikey_lifecycle.start_idle_sweeper(get_settings().api_key_idle_sweep_interval_seconds)

    logger.info("应用初始化完成")

    yield

    # ---- 关闭 ----
    from services.codex_channel import health as codex_health

    codex_health.stop_watchdog()
    from services import apikey_lifecycle

    apikey_lifecycle.stop_idle_sweeper()
    from services.project_repository import stop_repository_tasks
    await stop_repository_tasks()
    await cache_manager.close()
    await close_db_engine()
    logger.info("应用资源清理完成")


def create_app() -> FastAPI:
    """创建 FastAPI 应用实例"""
    settings = get_settings()

    if settings.is_prod:
        app = FastAPI(
            title="Audit Gateway",
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
            lifespan=lifespan,
        )
        allow_origins = settings.cors_origin_list or [
            "https://your-domain.com"]
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allow_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )
    else:
        app = FastAPI(title="Audit Gateway (dev)", lifespan=lifespan)
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    from utils.body_limit import RequestBodyLimitMiddleware
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=settings.request_body_max_bytes)

    # 请求上下文（request_id + 访问日志）
    app.add_middleware(RequestContextMiddleware)

    # 全局异常处理
    register_exception_handlers(app)

    from fastapi.responses import JSONResponse
    from services.enterprise_sso import SSOError

    @app.exception_handler(SSOError)
    async def handle_sso_error(_request: Request, exc: SSOError):
        headers = {"Cache-Control": "no-store"}
        if exc.code == "invalid_token":
            headers["WWW-Authenticate"] = 'Bearer error="invalid_token"'
        return JSONResponse({"error": exc.code, "error_description": exc.message},
                            status_code=exc.status, headers=headers)

    # 挂载静态文件
    os.makedirs("static/avatar", exist_ok=True)
    app.mount("/static", StaticFiles(directory="static"), name="static")

    # 注册路由
    app.include_router(health.router)
    app.include_router(admin.router)
    app.include_router(client.router)
    # OpenAI 兼容转发：审计、脱敏、用量统计的唯一写入点
    app.include_router(v1.router)
    app.include_router(open_api.router)
    from router.sso import admin_router as sso_admin_router, router as sso_router
    app.include_router(sso_router)
    app.include_router(sso_admin_router)
    from router.sites import router as sites_router
    app.include_router(sites_router)

    # 管理后台前端。必须**最后**挂：下面那个 catch-all 会吃掉所有没被上面
    # 匹配到的 GET，先挂就把 API 全遮住了。
    mount_frontend(app)

    return app


def mount_frontend(app: FastAPI) -> bool:
    """把 client/dist 挂上去，让网关自己发前端，省掉一层 nginx。

    找不到构建产物就跳过（开发期前端跑 vite dev server，自带代理）。
    返回是否真的挂上了，便于启动日志说清楚。
    """
    dist = resolve_frontend_dist(
        get_settings().frontend_dist_path, Path(__file__).resolve().parent
    )
    if dist is None:
        logger.info("未找到前端构建产物，跳过静态托管（开发期用 vite dev server 即可）")
        return False

    # /assets 单独挂 StaticFiles：命中不了就是真 404，不进 SPA 兜底。
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="frontend-assets")

    index_file = dist / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa(full_path: str):
        # 先看看是不是 dist 根下实实在在的文件（favicon.ico、robots.txt…）。
        candidate = (dist / full_path).resolve()
        try:
            candidate.relative_to(dist.resolve())
        except ValueError:
            # ../ 穿越。构造得出来，所以要挡 —— 否则这就是任意文件读取。
            raise NotFoundError("资源不存在")
        if full_path and candidate.is_file():
            return FileResponse(candidate)

        if not should_serve_spa(full_path):
            raise NotFoundError("资源不存在")

        # history 模式下的前端路由：回 index.html，让前端自己接管地址栏。
        # no-store：index.html 里带着本次构建的资源指纹，被缓存住的话，
        # 前端发了新版而用户还在拿旧的 index，去请求已经不存在的 assets。
        return FileResponse(index_file, headers={"Cache-Control": "no-store"})

    logger.info(f"已托管管理后台前端：{dist}")
    return True


# ==================== 模块级别 app 实例（Gunicorn 使用） ====================

app = create_app()

# ==================== 主程序入口 ====================

if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
        # 访问日志由 RequestContextMiddleware 统一打，关掉避免重复
        access_log=False,
    )

# Gunicorn 启动命令:
# gunicorn -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 main:app
