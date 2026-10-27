#!/usr/bin/env python3
"""Codex 渠道登录小工具 —— 浏览器授权的两步式流程

登录的本体是「**人**在浏览器里对 OpenAI 授权」。这个工具负责其余部分：
生成授权 URL（state/verifier 落临时文件）、兑换授权码、把凭证加密写进
渠道表，最后自动做一次连通探测。全程不接触账号密码、扫码与验证码。

用法（必须在 server/ 目录下跑，同 reset_account_password.py 的缘故）：

    python scripts/codex_login.py start
    #   → 打印授权 URL；浏览器打开并授权后，把地址栏整段回调 URL 复制下来

    python scripts/codex_login.py exchange --code '<回调URL>' --provider-code codex-main
    #   → 兑换凭证、加密入库、自动连通探测

    python scripts/codex_login.py import-rt --refresh-token '<RT>' --provider-code codex-main
    #   → 已有 refresh_token 时直接导入（跳过浏览器授权）

可选：--name 渠道展示名（默认取账号邮箱）、--base-url 自建中转（codex://host[/path]）。
"""

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SESSION_FILE = "/tmp/codex_login_session.json"
CODEX_BASE_DEFAULT = "codex://chatgpt"


def cmd_start(args) -> int:
    from services.codex_channel.oauth import start_login

    session_id, auth_url = start_login(args.redirect_uri)
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump({"session_id": session_id, **_pending_dump()}, f)
    os.chmod(SESSION_FILE, 0o600)

    print("第一步：在浏览器打开下面的授权 URL，用 ChatGPT/Codex 账号授权：")
    print()
    print("   " + auth_url)
    print()
    print("授权后浏览器会跳到 http://localhost:1455/auth/callback?code=...（页面打不开属正常），")
    print("把【地址栏整段 URL】复制下来，然后：")
    print()
    print(f"   python scripts/codex_login.py exchange --code '<粘这里>' --provider-code <渠道编码>")
    print()
    print("（登录状态已存到", SESSION_FILE + "，30 分钟内有效）")
    return 0


def _pending_dump() -> dict:
    """从内存会话里取 state/verifier 落盘（跨进程兑换要用）"""
    from services.codex_channel import oauth as codex_oauth

    sess = codex_oauth._sessions
    with sess._lock:
        items = dict(sess._items)
    # start_login 刚写的那个就是最新的
    latest = max(items.values(), key=lambda s: s.created_at)
    return {
        "state": latest.state,
        "code_verifier": latest.code_verifier,
        "redirect_uri": latest.redirect_uri,
    }


async def _upsert(provider_code: str, name: str, base_url: str, cred_json: str) -> tuple:
    from db import close_db_engine, init_db_engine
    from db.inserts.catalog import insert_provider
    from db.selects.catalog import get_provider_by_code
    from db.updates.catalog import update_provider

    await init_db_engine()
    try:
        existing = await get_provider_by_code(provider_code)
        if existing is not None:
            await update_provider(existing.id, api_key=cred_json)
            return existing.id, True
        provider_id = await insert_provider(
            code=provider_code,
            name=name,
            base_url=base_url,
            api_key=cred_json,
            timeout_seconds=120,
            remark="Codex 渠道",
        )
        return provider_id, False
    finally:
        await close_db_engine()


async def _probe(provider_id: int, base_url: str) -> None:
    from db import close_db_engine, init_db_engine
    from db.selects.catalog import get_provider
    from services.codex_channel import (CodexUpstreamError, ensure_access_token,
                                        load_credential, probe_models)

    await init_db_engine()
    try:
        provider = await get_provider(provider_id)
        if provider is None:
            print("[warn] 渠道行不存在，跳过探测")
            return
        _, cred = await ensure_access_token(provider.id, provider.api_key_encrypted)
        manifest = await probe_models(cred, base_url, provider.timeout_seconds or 15)
        models = manifest.get("models") or []
        print(f"[ok]   连通成功，账号可用模型 {len(models)} 个：")
        for m in models[:30]:
            print("         -", m)
    except (CodexUpstreamError, Exception) as exc:  # noqa: BLE001 — 探测失败要原样展示
        print(f"[warn] 连通探测失败：{exc}")
    finally:
        await close_db_engine()


async def _finish(tokens, identity, args) -> int:
    from services.codex_channel.credentials import CodexCredential

    cred = CodexCredential(tokens=tokens, identity=identity)
    name = (args.name or "").strip() or identity.email or f"Codex-{args.provider_code}"
    base_url = (args.base_url or "").strip() or CODEX_BASE_DEFAULT
    if not base_url.lower().startswith("codex://"):
        print("错误：--base-url 需以 codex:// 开头（如 codex://chatgpt）")
        return 2

    provider_id, updated = await _upsert(args.provider_code, name, base_url.rstrip("/"), cred.to_json())
    print(f"[ok]   渠道{'已更新' if updated else '已创建'}: id={provider_id} code={args.provider_code}")
    print(f"       账号: {identity.email or '?'}  套餐: {identity.plan_type or '?'}")
    await _probe(provider_id, base_url.rstrip("/"))
    return 0


async def cmd_exchange(args) -> int:
    from services.codex_channel.oauth import exchange_code_with

    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            sess = json.load(f)
    except FileNotFoundError:
        print(f"错误：找不到登录会话（{SESSION_FILE}），请先跑 start")
        return 2

    tokens, identity = await exchange_code_with(
        sess["code_verifier"], sess["redirect_uri"], args.code
    )
    os.remove(SESSION_FILE)
    return await _finish(tokens, identity, args)


async def cmd_import_rt(args) -> int:
    from services.codex_channel.oauth import refresh_tokens

    tokens, identity = await refresh_tokens(args.refresh_token)
    return await _finish(tokens, identity, args)


async def cmd_probe(args) -> int:
    from db import close_db_engine, init_db_engine
    from db.selects.catalog import get_provider

    await init_db_engine()
    try:
        provider = await get_provider(args.provider_id)
        if provider is None:
            print(f"错误：渠道 {args.provider_id} 不存在")
            return 2
        await _probe(provider.id, provider.base_url)
        return 0
    finally:
        await close_db_engine()


async def cmd_usage(args) -> int:
    from db import close_db_engine, init_db_engine
    from db.selects.catalog import get_provider
    from services.codex_channel import (CodexUpstreamError, ensure_access_token,
                                        fetch_usage, load_credential)

    await init_db_engine()
    try:
        provider = await get_provider(args.provider_id)
        if provider is None:
            print(f"错误：渠道 {args.provider_id} 不存在")
            return 2
        try:
            _, cred = await ensure_access_token(provider.id, provider.api_key_encrypted)
        except Exception as exc:  # noqa: BLE001
            print(f"[掉号] 凭证不可用：{exc}")
            return 1
        try:
            usage = await fetch_usage(cred, provider.base_url, provider.timeout_seconds or 30)
        except CodexUpstreamError as exc:
            print(f"[warn] 额度查询失败：{exc}")
            return 1
        print(f"账号: {usage['email'] or '?'}  套餐: {usage['plan_type'] or '?'}")
        for win in (usage["primary_window"], usage["secondary_window"]):
            if not win or not win.get("window_seconds"):
                continue
            hours = win["window_seconds"] // 3600
            secs = win.get("reset_after_seconds") or 0
            days, rem = divmod(secs, 86400)
            h, m = rem // 3600, rem % 3600 // 60
            reset = (f"{days}天{h}小时" if days else f"{h}小时{m}分")
            print(f"{hours}h 窗口: 已用 {win['used_percent']:.1f}%  约 {reset} 后重置")
        print(f"限速状态: {'已达上限' if usage['limit_reached'] else '正常'}"
              + (f"  重置券: {usage['reset_credits']['available_count']} 张" if usage['reset_credits'] else ""))
        return 0
    finally:
        await close_db_engine()


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex 渠道登录小工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start", help="生成授权 URL")
    p.add_argument("--redirect-uri", default=None)
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser("exchange", help="用回调 URL/授权码兑换凭证并入库")
    p.add_argument("--code", required=True, help="授权后粘贴的整条回调 URL 或 code")
    p.add_argument("--provider-code", required=True)
    p.add_argument("--name", default=None)
    p.add_argument("--base-url", default=None)
    p.set_defaults(fn=cmd_exchange)

    p = sub.add_parser("probe", help="连通探测（拉账号真实模型清单）")
    p.add_argument("--provider-id", type=int, required=True)
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("usage", help="查账号剩余额度（5h/7d 窗口）")
    p.add_argument("--provider-id", type=int, required=True)
    p.set_defaults(fn=cmd_usage)

    p = sub.add_parser("import-rt", help="直接导入 refresh_token")
    p.add_argument("--refresh-token", required=True)
    p.add_argument("--provider-code", required=True)
    p.add_argument("--name", default=None)
    p.add_argument("--base-url", default=None)
    p.set_defaults(fn=cmd_import_rt)

    args = parser.parse_args()
    if args.cmd == "start":
        return args.fn(args)
    return asyncio.run(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
