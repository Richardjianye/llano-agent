"""上游 usage 字段探针 —— 回答「到底能不能按缓存计价」

## 为什么需要单独探一次

要不要在「编辑模型」里加缓存读取单价，取决于一个纯事实问题：
**上游到底返不返回缓存命中的 token 数。**

这个问题不能靠猜，也不能靠读厂商文档 —— 我们打的是合作厂商的 NewAPI 网关，
中间转发层完全可能把 usage 里的扩展字段吃掉。文档说有，不等于我们这条链路上有。

配了单价却拿不到缓存 token，报表会一直按「全价」算，金额虚高且无人察觉；
反过来，能拿到却不配单价，就是白白多花钱还不知道省在哪。

## 两路取证

1. **翻旧账**：`usage_records.usage_raw` 存着历史调用的 usage 原文（决策 #21
   当初坚持全量入库，就是为了这种时候）。直接扫一遍，看都出现过哪些字段。
2. **打新的**：连发两次**前缀完全相同的长请求**。上游若有上下文缓存，
   第二次会命中，缓存字段就会出现在 usage 里。第一次不命中是正常的。

## 怎么跑

    cd audit-gateway/server

    # 只翻旧账（不花钱，不需要网关在跑，直接读库）
    python scripts/probe_upstream_usage.py --db-only

    # 翻旧账 + 打两次真实请求（会产生少量费用）
    python scripts/probe_upstream_usage.py \
        --user admin --password '...' --model doubao-seed-2-0-lite-260428

## 结论怎么读

脚本最后会打印一张「字段 → 是否出现过」的表。判断依据：

- 出现 `cached_tokens` / `prompt_cache_hit_tokens` / `cache_read_input_tokens`
  → **能按缓存计价**，去「编辑模型」把缓存读取单价填上
- 一个都没有 → 缓存计价配了也是摆设，先别填，省得报表出现算不准的金额
- 出现没见过的字段 → 贴出来，可能是厂商私有扩展，值得纳入归一化
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional, Set

# 让脚本能 `import db` / `import model`：直接跑 `python scripts/xxx.py` 时
# sys.path[0] 是 scripts/ 而不是 server/，不补这一行就会 ModuleNotFoundError
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 已知的缓存/计费相关字段名 ----
# 各家命名不统一，这里把见过的都列上；探针的价值恰恰在于发现**不在这张表里**的字段
KNOWN_CACHE_FIELDS = {
    "cached_tokens": "缓存命中（OpenAI 兼容，在 prompt_tokens_details 下）",
    "prompt_cache_hit_tokens": "缓存命中（DeepSeek 风格）",
    "prompt_cache_miss_tokens": "缓存未命中（DeepSeek 风格）",
    "cache_read_input_tokens": "缓存读取（Anthropic 风格）",
    "cache_creation_input_tokens": "缓存写入（Anthropic 风格）",
    "cached_prompt_tokens": "缓存命中（部分网关的叫法）",
    "cache_hit_tokens": "缓存命中（豆包/火山风格）",
    "cache_miss_tokens": "缓存未命中（豆包/火山风格）",
}

KNOWN_OTHER_FIELDS = {
    "prompt_tokens": "输入 token",
    "completion_tokens": "输出 token",
    "total_tokens": "合计 token",
    "reasoning_tokens": "推理 token",
    "prompt_tokens_details": "输入细分（嵌套）",
    "completion_tokens_details": "输出细分（嵌套）",
}

PASS, FAIL, INFO = "✅", "❌", "  "


def flatten_keys(obj: Any, prefix: str = "") -> Set[str]:
    """把嵌套 dict 的键拍平成 a.b.c 形式

    缓存字段常藏在 `prompt_tokens_details.cached_tokens` 这种二层结构里，
    只看顶层键会漏掉。
    """
    keys: Set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}{key}"
            keys.add(path)
            keys |= flatten_keys(value, f"{path}.")
    return keys


# ==================== 一、翻旧账 ====================


async def scan_database(limit: int = 500) -> List[Dict[str, Any]]:
    """扫 usage_records.usage_raw，把见过的 usage 原文捞出来

    决策 #21 当初坚持「认不出来的字段也全量入库」，为的就是这一刻：
    不必重跑历史，直接拿原始数据回答问题。
    """
    from sqlalchemy import desc, select

    from db import init_async_session
    from model.database import UsageRecord

    session_maker = await init_async_session()
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(UsageRecord.model_name, UsageRecord.usage_raw)
                .where(UsageRecord.usage_raw.isnot(None))
                .order_by(desc(UsageRecord.id))
                .limit(limit)
            )
        ).all()

    samples = []
    for model_name, raw in rows:
        try:
            samples.append({"model": model_name, "usage": json.loads(raw)})
        except (json.JSONDecodeError, TypeError):
            continue
    return samples


# ==================== 二、打新的 ====================

# 缓存要命中，前缀必须足够长且**逐字相同**。
# 各家的最小缓存粒度不一样（常见 256~1024 token），这里堆到几千字符稳妥些。
CACHE_PREFIX = (
    "以下是公司内部 AI 使用规范，请在回答任何问题前先完整阅读并遵守。\n\n"
    + "\n".join(
        f"第 {i} 条：员工在使用 AI 工作台时，应确保输入内容不含客户隐私信息、"
        f"不含未公开的财务数据、不含涉及商业秘密的技术细节；"
        f"如需引用，请先做脱敏处理后再提交。"
        for i in range(1, 41)
    )
)


async def probe_live(
    base_url: str, user: str, password: str, model: str
) -> List[Dict[str, Any]]:
    """连发两次相同前缀的请求，把两次的 usage 原文都取回来"""
    try:
        import httpx
    except ImportError:
        sys.exit("缺少 httpx，请先 pip install -r requirements.txt")

    samples: List[Dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            f"{base_url}/admin/user/login",
            json={"username": user, "password": password},
        )
        body = resp.json()
        if body.get("code") != 0:
            sys.exit(f"登录失败：{body.get('message')}")
        token = body["data"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        for attempt in (1, 2):
            print(f"{INFO}第 {attempt} 次请求（前缀 {len(CACHE_PREFIX)} 字符）…")
            resp = await client.post(
                f"{base_url}/v1/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": CACHE_PREFIX},
                        {"role": "user", "content": "用一句话概括上面的规范。"},
                    ],
                },
            )

            try:
                payload = resp.json()
            except ValueError:
                print(f"{FAIL} 响应不是 JSON（HTTP {resp.status_code}）：{resp.text[:200]}")
                continue

            # ---- OpenAI 风格的错误 ----
            if isinstance(payload, dict) and "error" in payload:
                err = payload["error"]
                msg = err.get("message") if isinstance(err, dict) else str(err)
                print(f"{FAIL} 上游/网关返回错误：{msg}")
                continue

            # ---- 网关自己的 {code,message,data} 信封 ----
            # 转发接口正常时**不该**返回这种结构（客户端 SDK 认不出来）。
            # 出现它基本等于网关内部 500 了 —— 第一版探针没认这种形态，
            # 只打印「没有 usage 字段」，把一个明确的服务端错误说成了上游能力问题
            if isinstance(payload, dict) and "code" in payload and "usage" not in payload:
                print(
                    f"{FAIL} 网关内部错误（HTTP {resp.status_code}）"
                    f" code={payload.get('code')}：{payload.get('message')}"
                )
                print(f"{INFO}  → 这不是上游的问题。最常见原因是**迁移没跑全**：")
                print(f"{INFO}    ORM 里的列在库里不存在，任何查 models 表的操作都会炸。")
                print(f"{INFO}    先把 scripts/migrations/ 下的三份按顺序跑完再回来。")
                continue

            usage = payload.get("usage") if isinstance(payload, dict) else None
            if not isinstance(usage, dict):
                print(f"{FAIL} 响应里没有 usage 字段（HTTP {resp.status_code}）")
                # 把响应原样打出来 —— 「没有 usage」可能是上游不返回，
                # 也可能是响应压根不是对话补全结果，两者的处置完全不同
                print(f"{INFO}  响应原文：{json.dumps(payload, ensure_ascii=False)[:400]}")
                continue

            samples.append({"model": model, "usage": usage, "attempt": attempt})
            print(f"{INFO}  usage = {json.dumps(usage, ensure_ascii=False)}")

            if attempt == 1:
                # 给上游一点时间把缓存建起来；太快连发有些实现不会命中
                await asyncio.sleep(3)

    return samples


# ==================== 三、下结论 ====================


def report(samples: List[Dict[str, Any]]) -> int:
    print(f"\n{'=' * 64}\n结论\n{'=' * 64}")

    if not samples:
        print(f"{FAIL} 一份 usage 样本都没拿到，无法判断。")
        print("   → 先确认库里有调用记录，或带上 --user/--password 打一次真实请求")
        return 1

    all_keys: Set[str] = set()
    for sample in samples:
        all_keys |= flatten_keys(sample["usage"])

    print(f"{INFO}共分析 {len(samples)} 份 usage 样本，出现过的字段：\n")
    for key in sorted(all_keys):
        leaf = key.split(".")[-1]
        label = KNOWN_CACHE_FIELDS.get(leaf) or KNOWN_OTHER_FIELDS.get(leaf) or ""
        mark = "💰" if leaf in KNOWN_CACHE_FIELDS else "  "
        print(f"  {mark} {key:<48} {label}")

    cache_keys = {k for k in all_keys if k.split(".")[-1] in KNOWN_CACHE_FIELDS}

    # 光有字段名不够 —— 全是 0 说明链路支持但这次没命中，也说明字段是通的
    nonzero = []
    for sample in samples:
        flat = _flatten_values(sample["usage"])
        for key, value in flat.items():
            if key.split(".")[-1] in KNOWN_CACHE_FIELDS and isinstance(value, (int, float)) and value > 0:
                nonzero.append((key, value))

    print()
    if not cache_keys:
        print(f"{FAIL} **上游没有返回任何缓存相关字段**")
        print("   → 「编辑模型」里的缓存读取单价先别填：配了也没有 token 数可乘，")
        print("     报表会一直按全价算，金额虚高且没人会察觉。")
        print("   → 建议在该字段旁标注「上游未返回缓存数据，填了不生效」。")
        return 0

    print(f"{PASS} **上游返回了缓存字段**：{', '.join(sorted(cache_keys))}")
    if nonzero:
        print(f"{PASS} 且确实出现过非零值：{nonzero[:5]}")
        print("   → 可以按缓存计价，去「编辑模型」把缓存读取单价填上。")
    else:
        print(f"{INFO} 但本次采样全是 0 —— 字段是通的，只是没命中缓存。")
        print("   → 仍然可以配单价；命中时自然会算进去。")
        print("   → 想确认命中，把 --model 换成明确支持上下文缓存的模型再跑一次。")

    unknown = {
        k for k in all_keys
        if k.split(".")[-1] not in KNOWN_CACHE_FIELDS
        and k.split(".")[-1] not in KNOWN_OTHER_FIELDS
    }
    if unknown:
        print(f"\n{INFO}另外发现未归一化的字段（可能是厂商私有扩展）：")
        for key in sorted(unknown):
            print(f"     {key}")
        print("   → 这些目前只躺在 usage_raw 里。若涉及计费，值得加进")
        print("     services/usage.py 的 extract_breakdown 做归一化。")

    return 0


def _flatten_values(obj: Any, prefix: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}{key}"
            if isinstance(value, dict):
                out.update(_flatten_values(value, f"{path}."))
            else:
                out[path] = value
    return out


async def main() -> int:
    parser = argparse.ArgumentParser(description="探明上游 usage 里有没有缓存字段")
    parser.add_argument("--base-url", default="http://localhost:7899")
    parser.add_argument("--user", help="登录用户名；不填则只翻旧账")
    parser.add_argument("--password")
    parser.add_argument(
        "--model",
        default="doubao-seed-2-0-lite-260428",
        help="用于探测的模型",
    )
    parser.add_argument("--db-only", action="store_true", help="只扫库，不发请求")
    args = parser.parse_args()

    samples: List[Dict[str, Any]] = []

    print(f"{'=' * 64}\n① 翻旧账：扫 usage_records.usage_raw\n{'=' * 64}")
    try:
        db_samples = await scan_database()
        print(f"{INFO}从库里捞到 {len(db_samples)} 条带 usage 原文的记录")
        samples.extend(db_samples)
    except Exception as exc:
        print(f"{FAIL} 读库失败（不影响后面打真实请求）：{exc}")

    if not args.db_only and args.user and args.password:
        print(f"\n{'=' * 64}\n② 打新的：连发两次相同长前缀\n{'=' * 64}")
        samples.extend(
            await probe_live(args.base_url, args.user, args.password, args.model)
        )
    elif not args.db_only:
        print(f"\n{INFO}未提供 --user/--password，跳过实时探测")

    return report(samples)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
