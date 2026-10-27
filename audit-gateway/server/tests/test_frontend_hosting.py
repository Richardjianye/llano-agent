"""网关自己托管管理后台前端（不再需要 nginx），以及 prod 下的日志 sink。

两件事凑一个文件，因为它们是同一次上线暴露出来的，而且失败形态是同一类：
**没有任何报错，只是东西不在了**。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from services.frontend import (is_api_path, looks_like_asset,
                               resolve_frontend_dist, should_serve_spa)
from utils.middleware import register_exception_handlers


# ── 该不该回 index.html ──────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/audit/list", "/dashboard", "/users/7/edit", "/"])
def test_frontend_routes_fall_back_to_index(path):
    """history 模式下这些是前端自己的路由，服务端没有对应文件。

    不兜底的话表现是「在这个页面按 F5 就白屏」，而第一次进入一切正常 ——
    最容易被当成偶发。
    """
    assert should_serve_spa(path) is True


@pytest.mark.parametrize(
    "path",
    ["/admin/skill-events/list", "/v1/chat/completions", "/client/model",
     "/healthz", "/static/avatar/x.png", "/admin", "/v1"],
)
def test_api_paths_never_fall_back(path):
    """打错的接口必须回 404 JSON，不能回 index.html。

    否则前端拿一坨 HTML 去 JSON.parse，报的是「Unexpected token <」——
    那句话既不说路径错了，也不说错在哪一段。
    """
    assert is_api_path(path) is True
    assert should_serve_spa(path) is False


@pytest.mark.parametrize(
    "path", ["/assets/index-abc.js", "/assets/main.css", "/favicon.ico", "/x.woff2"]
)
def test_missing_assets_never_fall_back(path):
    """资源找不到就该 404。

    回 index.html 的话浏览器拿到 Content-Type: text/html 的「JavaScript」，
    控制台报 MIME 错误，而真正的原因是那个文件根本不在包里。
    """
    assert looks_like_asset(path) is True
    assert should_serve_spa(path) is False


def test_a_path_that_merely_starts_with_an_api_word_is_not_an_api_path():
    """`/administrators` 不是 `/admin` 下的东西。

    用 startswith 而不比对边界的话，前端多一个以 admin 开头的路由就会 404，
    且现象是「只有这一个页面刷新会白屏」。
    """
    assert is_api_path("/administrators") is False
    assert should_serve_spa("/administrators") is True


# ── 找不到产物时不能把服务带崩 ────────────────────────────────────────────

def test_missing_dist_is_skipped_not_fatal(tmp_path):
    """前端没构建时跳过托管，而不是启动失败 —— 开发期本来就跑 vite dev server。"""
    assert resolve_frontend_dist(None, tmp_path) is None
    assert resolve_frontend_dist(str(tmp_path / "nope"), tmp_path) is None


def test_a_dist_without_index_html_does_not_count(tmp_path):
    """目录在但 index.html 不在 —— 构建到一半、或者拷贝漏了。

    当成「有产物」挂上去的话，后台是一片空白而不是明确的「没托管」。
    """
    dist = tmp_path / "client" / "dist"
    dist.mkdir(parents=True)
    (dist / "assets").mkdir()

    assert resolve_frontend_dist(None, tmp_path / "server") is None


def test_a_complete_dist_is_found_next_to_the_server(tmp_path):
    dist = tmp_path / "client" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")

    assert resolve_frontend_dist(None, tmp_path / "server") == dist


# ── 端到端 ────────────────────────────────────────────────────────────────

def _app_with_dist(tmp_path, monkeypatch) -> FastAPI:
    import main

    dist = tmp_path / "client" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>后台</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (dist / "favicon.ico").write_bytes(b"\x00")

    app = FastAPI()
    register_exception_handlers(app)

    from router.v1 import router as v1_router
    app.include_router(v1_router)

    # 要打 main.get_settings，不是 config.get_settings：main 里是
    # `from config import get_settings`，名字已经绑到 main 的命名空间上了，
    # 改 config 那个不会影响它 —— 打错的话这里会挂上**真实的** client/dist，
    # 而测试照样跑得动，只是测的东西全不对。
    class _S:
        frontend_dist_path = str(dist)

    monkeypatch.setattr(main, "get_settings", lambda: _S())
    main.mount_frontend(app)
    return app


def test_deep_link_returns_the_spa_shell(tmp_path, monkeypatch):
    response = TestClient(_app_with_dist(tmp_path, monkeypatch)).get("/audit/list")

    assert response.status_code == 200
    assert "后台" in response.text


def test_the_shell_is_not_cached(tmp_path, monkeypatch):
    """index.html 里带着本次构建的资源指纹。被缓存住的话，发了新版而用户还在拿
    旧的 index，去请求已经不存在的 assets —— 表现是「清了缓存就好了」。"""
    response = TestClient(_app_with_dist(tmp_path, monkeypatch)).get("/audit/list")

    assert response.headers["cache-control"] == "no-store"


def test_real_files_are_served_as_themselves(tmp_path, monkeypatch):
    client = TestClient(_app_with_dist(tmp_path, monkeypatch))

    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/favicon.ico").status_code == 200


def test_an_unknown_api_path_does_not_get_the_spa(tmp_path, monkeypatch):
    response = TestClient(_app_with_dist(tmp_path, monkeypatch)).get("/v1/nope")

    assert "后台" not in response.text


def test_path_traversal_is_refused(tmp_path, monkeypatch):
    """`/../config.py` 是构造得出来的。挡不住就是任意文件读取 ——
    而这个进程读得到 .env。"""
    client = TestClient(_app_with_dist(tmp_path, monkeypatch))
    (tmp_path / "client" / "secret.txt").write_text("SECRET", encoding="utf-8")

    for attempt in ["/../secret.txt", "/..%2fsecret.txt", "/%2e%2e/secret.txt"]:
        response = client.get(attempt)
        assert "SECRET" not in response.text, attempt


# ── prod 下必须还有一个 sink ──────────────────────────────────────────────

def test_prod_logging_still_has_a_sink_without_gunicorn():
    """真实事故：prod 分支先 logger.remove()，再**只在 gunicorn.error 有 handler
    时**才把 sink 加回来。用 `uvicorn main:app` 起服务时 gunicorn 不在，
    于是全部 loguru 日志静默丢弃 —— 包括那条只打印一次的总裁初始密码。

    丢日志本身够糟，更糟的是它看上去像「服务很安静」：uvicorn 自己的日志照常刷，
    端口照常监听，请求照常返回。
    """
    import logging

    import main
    from loguru import logger

    # 确认 gunicorn 真的不在场，否则这条测的是另一种情况
    logging.getLogger("gunicorn.error").handlers.clear()

    try:
        main.setup_loguru(environment="prod")
        assert len(logger._core.handlers) > 0, "prod 下一个 sink 都没有 —— 日志全丢"
    finally:
        logger.remove()
        logger.add(lambda _: None)


def test_non_prod_leaves_the_default_sink_alone():
    import main
    from loguru import logger

    before = len(logger._core.handlers)
    main.setup_loguru(environment="dev")

    assert len(logger._core.handlers) == before
