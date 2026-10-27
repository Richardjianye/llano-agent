"""登记上游模型渠道并同步模型清单

地址与 Key 从 .env.dev / .env.prod 读（`NEWAPI_BASE_URL` / `NEWAPI_API_KEY`），
这两个文件已在 .gitignore 里。也可以用命令行参数临时覆盖。

用法（在 server/ 目录下）：

    python scripts/bootstrap.py                       # 读 .env
    python scripts/bootstrap.py --api-key sk-xxx      # 临时覆盖

做了什么：
1. 幂等登记上游渠道（API Key 用 PROVIDER_ENCRYPTION_KEY 加密入库）
2. 调 {base_url}/v1/models 自动发现模型并登记，已存在的跳过
   —— 上游不支持该接口时，用 --models 手工指定

登记完成后 Key 已在库里，改 Key 可以直接走后台「模型与白名单 → 上游渠道」。

**不预置任何白名单**：按你的要求，模型开通逐部门在后台手工配置。
在此之前所有人都没有可用模型（白名单是默认拒绝），这是预期行为。

部门与总裁账号由 db.init_db() 在建表时自动写入，本脚本不重复处理。
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import get_settings  # noqa: E402
from db import close_db_engine, init_db, init_db_engine  # noqa: E402
from db.inserts.catalog import insert_model, insert_provider  # noqa: E402
from db.selects.catalog import (find_model_by_ref,  # noqa: E402
                                get_provider_by_code)
from db.updates.catalog import update_provider  # noqa: E402

DEFAULT_CODE = "newapi"
DEFAULT_NAME = "合作厂商 NewAPI 网关"


def discover_models(base_url: str, api_key: str, insecure: bool = False) -> list[str]:
    """调用 OpenAI 兼容的 /v1/models 拿模型清单

    用 httpx 而不是 urllib：httpx 自带 certifi 根证书。
    macOS 上从 python.org 装的 Python 默认没装系统根证书，
    urllib 会直接 CERTIFICATE_VERIFY_FAILED。
    """
    url = f"{base_url.rstrip('/')}/v1/models"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        import httpx
    except ImportError:
        print("[warn] 未安装 httpx，跳过自动发现。执行 pip install -r requirements.txt")
        return []

    try:
        with httpx.Client(timeout=20, verify=not insecure) as client:
            resp = client.get(url, headers=headers)
    except httpx.ConnectError as exc:
        if "CERTIFICATE_VERIFY" in str(exc) or "SSL" in str(exc):
            print(f"[warn] SSL 证书校验失败：{exc}")
            print("       macOS 上通常是 Python 缺根证书，二选一：")
            print('       1) 运行 "/Applications/Python 3.x/Install Certificates.command"')
            print("       2) 临时加 --insecure 跳过校验（只在确认上游可信时用）")
        else:
            print(f"[warn] 连接上游失败：{exc}")
        return []
    except Exception as exc:
        print(f"[warn] 拉取模型清单失败：{exc}")
        return []

    if resp.status_code == 401:
        print("[warn] 上游返回 401，API Key 无效或已过期")
        return []
    if resp.status_code != 200:
        print(f"[warn] 拉取模型清单失败：HTTP {resp.status_code} {resp.text[:200]}")
        return []

    try:
        payload = resp.json()
    except ValueError:
        print(f"[warn] 上游返回的不是 JSON：{resp.text[:200]}")
        return []

    data = payload.get("data") or []
    names = [
        item.get("id") for item in data
        if isinstance(item, dict) and item.get("id")
    ]
    return sorted(set(names))


async def ensure_provider(code: str, name: str, base_url: str, api_key: str) -> int:
    provider = await get_provider_by_code(code)
    if provider:
        # 已存在则只更新地址与 Key，不动其他配置
        await update_provider(provider.id, base_url=base_url.rstrip("/"), api_key=api_key)
        print(f"[ok]   渠道已存在，已更新地址与 Key: {code} -> id={provider.id}")
        return provider.id

    provider_id = await insert_provider(
        code=code, name=name, base_url=base_url.rstrip("/"), api_key=api_key
    )
    print(f"[ok]   渠道已创建: {code} -> id={provider_id}")
    return provider_id


async def ensure_models(provider_id: int, model_names: list[str]) -> int:
    added = 0
    for model_name in model_names:
        if await find_model_by_ref(model_name):
            print(f"[skip] 模型已存在: {model_name}")
            continue
        model_id = await insert_model(
            provider_id=provider_id,
            model_name=model_name,
            display_name=model_name,
        )
        print(f"[ok]   模型已登记: {model_name} -> id={model_id}")
        added += 1
    return added


async def main(args) -> int:
    settings = get_settings()

    # 优先级：命令行参数 > .env 配置
    base_url = args.base_url or settings.newapi_base_url or ""
    api_key = args.api_key or settings.newapi_api_key or ""

    if not base_url:
        print("错误：未提供上游地址。在 .env.dev 配 NEWAPI_BASE_URL 或传 --base-url")
        return 1
    if not api_key:
        print("错误：未提供 API Key。在 .env.dev 配 NEWAPI_API_KEY 或传 --api-key")
        print("      （.env.dev / .env.prod 已在 .gitignore 中，不会被提交）")
        return 1

    model_names = args.models or discover_models(base_url, api_key, args.insecure)
    if not model_names:
        print("未能拿到模型清单。可用 --models deepseek-chat,deepseek-reasoner 手工指定")
        print("（渠道仍会登记，模型补齐后重跑本脚本即可，已存在的会跳过）")

    await init_db_engine()
    await init_db()

    provider_id = await ensure_provider(args.code, args.name, base_url, api_key)
    added = await ensure_models(provider_id, model_names) if model_names else 0

    await close_db_engine()

    print(f"\n完成：渠道 1 个，新增模型 {added} 个。")
    print("下一步：登录后台 → 模型与白名单 → 逐部门开通可用模型。")
    print("在配置白名单之前，所有人都没有可用模型（白名单默认拒绝）。")
    return 0


def parse_models(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="登记上游渠道并同步模型清单")
    parser.add_argument(
        "--base-url", default=None, help="上游地址，默认读 .env 的 NEWAPI_BASE_URL"
    )
    parser.add_argument(
        "--api-key", default=None, help="API Key，默认读 .env 的 NEWAPI_API_KEY"
    )
    parser.add_argument("--code", default=DEFAULT_CODE, help="渠道编码")
    parser.add_argument("--name", default=DEFAULT_NAME, help="渠道展示名")
    parser.add_argument(
        "--models",
        type=parse_models,
        default=None,
        help="逗号分隔的模型名，指定后跳过自动发现",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="跳过 SSL 证书校验（本机缺根证书时的临时手段，仅对可信上游使用）",
    )
    sys.exit(asyncio.run(main(parser.parse_args())))
