"""端到端实测：一次跑通「转发 → 审计存储 → 附件 → 查阅日志 → 计费」

## 这个脚本回答什么问题

单测只能证明纯逻辑对。真正会出事的地方是它们的接缝：数据库结构与 ORM 漂移、
多模态请求上游不收、流式拿不到 usage、附件落盘目录没权限——
这些全都要真发一次请求才暴露得出来。

所以这里**模拟客户端经网关打真实上游**，然后回头用管理端接口核对四条链路：

| # | 检查 | 出问题的典型表现 |
| - | ---- | ---------------- |
| 1 | 会话原文存了、读得出来 | 详情接口 500 或正文是半串 base64 |
| 2 | 图片/文件抽成了附件记录并落盘 | attachment_count=0，或 stored=false |
| 3 | 查阅行为写了日志 | access-logs 里没有对应的 detail/attachment 行 |
| 4 | 用量与 token 细分入库 | usage 全 0，或 tokens_estimated=true（上游没回 usage） |
| 5 | 中断的调用照样计费且不算失败 | 少一笔账（漏计费），或失败率被污染 |
| 6 | 流式/直接可区分、请求ID 能追到原文 | 排查时分不清是哪种调用 |

多模态用**豆包**（默认 `doubao-seed-2-0-lite-260428`）：它的图片、视频、
文档、音频**全部是 base64 内联**，不是多段文件上传。所以四类附件在网关这边
走的是同一条 content parts 通道，测试也按这个来。

## 怎么跑

    cd audit-gateway/server
    python scripts/e2e_audit_check.py --user admin --password '你的密码'

前置条件：

1. 网关已启动（`python main.py`），MySQL/Redis 正常
2. 数据库按当前 ORM 建表或重建完成；当前开发库不再重复执行历史迁移脚本
3. 上游渠道已登记且目标模型对该账号开通（`python scripts/bootstrap.py`）
4. 登录账号必须**在管理部门且级别 ≥ 审计员**，否则第 1、3 项查不了（决策 #8）

⚠️ 这个脚本会真的调用上游、真的产生费用（几个 token 的量级），
也会在库里留下真实的会话记录与查阅日志。别在生产库上反复跑。

## 为什么不做成 pytest

它依赖真实 MySQL、真实上游 Key 和真实网络，放进 `pytest` 会让
「跑单测」这件事变得不可靠 —— 那套是纯逻辑测试，必须能离线秒过。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import zlib
from typing import List, Optional

from e2e.gateway import Gateway
from e2e.reconcile import (
    check_access_logs,
    check_attachment_download,
    check_audit,
    check_export,
    check_usage,
)
from e2e.reporter import FAIL, PASS, Reporter


# ==================== 测试素材 ====================
# 内置而不是读外部文件：脚本要能在任何机器上直接跑，不依赖仓库里有没有某张图

def _tiny_png() -> bytes:
    """一张 16x16 的红色 PNG（上游要求宽高至少 14px）"""
    def chunk(kind: bytes, data: bytes) -> bytes:
        import struct
        payload = kind + data
        return (
            struct.pack(">I", len(data))
            + payload
            + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)
        )

    import struct
    ihdr = struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0)
    raw = (b"\x00" + b"\xff\x00\x00" * 16) * 16
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _tiny_wav() -> bytes:
    """一段极短的静音 WAV（44 字节头 + 少量采样）

    音频也走 base64 内联，所以内容是不是「真的能听」不重要，
    重要的是格式合法、能被抽成附件、能原样取回来。
    """
    import struct

    samples = b"\x00\x00" * 800  # 100ms，超过上游 50ms 下限
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, 8000, 16000, 2, 16)
    data = struct.pack("<4sI", b"data", len(samples)) + samples
    body = b"WAVE" + fmt + data
    return struct.pack("<4sI", b"RIFF", len(body)) + body


def _tiny_pdf() -> bytes:
    """现造一个合法单页 PDF，避免拿 CSV 冒充上游要求的 PDF。"""
    stream = b"BT /F1 12 Tf 50 100 Td (R&D uses more tokens: 320000) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 150] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    result.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(result)


def data_url(mime: str, payload: bytes) -> str:
    """统一转成 base64 data URL

    ⚠️ 豆包（doubao-*）系列的图片、视频、文档、音频**全部走 base64 内联**，
    不是「附加文件」那种多段上传。所以网关这边它们走的是同一条
    content parts 通道，附件抽取逻辑对四种类型是同一套 —— 测试也照这个来。
    """
    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


async def pick_model(gw: Gateway, reporter: Reporter, wanted: Optional[str]) -> str:
    """确认目标模型对当前账号可用

    白名单是「默认拒绝」（决策 #18），没开通的话这里就该停下，
    否则后面每一步都会撞 403 而让人以为是审计功能坏了。
    """
    resp = await gw.client.get(
        f"{gw.base_url}/v1/models", headers=gw._headers()
    )
    models = [m["id"] for m in resp.json().get("data", [])]

    if not models:
        sys.exit("当前账号没有任何可用模型 —— 请先在后台「模型与白名单」为其部门开通")

    reporter.info(f"可用模型：{', '.join(models)}")

    if wanted:
        if wanted in models:
            return wanted
        # 模糊匹配一下，省得为大小写或版本后缀纠结
        loose = [m for m in models if wanted.lower().replace(" ", "-") in m.lower()]
        if loose:
            reporter.warn(f"未精确匹配 {wanted}，改用 {loose[0]}")
            return loose[0]
        sys.exit(f"模型「{wanted}」不在可用列表里，请确认名称或先在后台开通")

    return models[0]


async def send_requests(
    gw: Gateway, reporter: Reporter, model: str
) -> tuple[int, Optional[str]]:
    """发四种请求：纯文本、带图片、带文件、流式。返回**实际发出的次数**

    分开发而不是塞进一次：出问题时能一眼看出是哪种内容形态引起的。

    返回次数是为了后面逐笔核对记账 —— 只查「有没有用量记录」是不够的，
    流式那次漏记时报表看起来完全正常（这个漏洞就是这么躲过第一版脚本的）。
    """
    reporter.section("① 模拟客户端发请求（真实上游）")

    results: Dict[str, Any] = {}

    # ---- 品牌客户端审计原文 / 模型脱敏双通道 ----
    policy = await gw.redaction_policy()
    keywords = policy.get("keywords") or []
    audit_sentinel: Optional[str] = None
    if keywords:
        audit_sentinel = f"{keywords[0]}-AUDIT-ONLY-STAGE3"
        placeholder = str(policy.get("placeholder") or "[公司]")
        reporter.info("发送双通道请求（模型副本脱敏，私有审计字段携带原文）…")
        dual_resp = await gw.chat(
            [{
                "role": "user",
                "content": f"{placeholder}-AUDIT-ONLY-STAGE3，只回答：双通道验证通过",
            }],
            model,
            audit_messages=[{
                "role": "user",
                "content": f"{audit_sentinel}，只回答：双通道验证通过",
            }],
            policy_version=str(policy.get("version") or ""),
        )
        ok = "choices" in dual_resp
        reporter.check(
            ok,
            "双通道请求转发成功",
            "" if ok else json.dumps(dual_resp, ensure_ascii=False)[:200],
        )
        reporter.check(
            audit_sentinel not in json.dumps(dual_resp, ensure_ascii=False),
            "模型响应未回显审计原文哨兵",
        )
        results["dual_channel"] = dual_resp
    else:
        reporter.warn("脱敏策略没有关键词，跳过双通道哨兵请求")

    # ---- 纯文本 ----
    reporter.info("发送纯文本请求…")
    text_resp = await gw.chat(
        [{"role": "user", "content": "用一句话说明什么是会话审计。"}], model
    )
    ok = "choices" in text_resp
    reporter.check(ok, "纯文本请求转发成功", "" if ok else json.dumps(text_resp, ensure_ascii=False)[:200])
    if ok:
        usage = text_resp.get("usage") or {}
        reporter.check(
            bool(usage.get("total_tokens")),
            "上游返回了 usage",
            f"prompt={usage.get('prompt_tokens')} completion={usage.get('completion_tokens')}",
        )
        results["text"] = text_resp

    # ---- 带图片 ----
    reporter.info("发送带图片请求（16x16 PNG，base64 内联）…")
    image_resp = await gw.chat(
        [{
            "role": "user",
            "content": [
                {"type": "text", "text": "这张图是什么颜色？只回答颜色。"},
                {"type": "image_url",
                 "image_url": {"url": data_url("image/png", _tiny_png())}},
            ],
        }],
        model,
    )
    if "choices" in image_resp:
        reporter.check(True, "带图片请求转发成功")
    else:
        # 上游模型不支持视觉是很常见的情况，不该算功能缺陷 ——
        # 但审计侧仍然必须记下这次请求（失败也要记账）
        err = (image_resp.get("error") or {}).get("message", "")
        reporter.warn("带图片请求被上游拒绝（多半是该模型不支持视觉）", err[:160])
        reporter.info("→ 审计与用量仍应记下这次失败调用，后面会核对")
    results["image"] = image_resp

    # ---- 带文件 ----
    reporter.info("发送带文件请求（PDF，base64 内联）…")
    file_resp = await gw.chat(
        [{
            "role": "user",
            "content": [
                {"type": "text", "text": "这份文件里的 token 数是多少？"},
                {"type": "file", "file": {
                    "filename": "usage-summary.pdf",
                    "file_data": data_url("application/pdf", _tiny_pdf()),
                }},
            ],
        }],
        model,
    )
    if "choices" in file_resp:
        reporter.check(True, "带文件请求转发成功")
    else:
        err = (file_resp.get("error") or {}).get("message", "")
        reporter.warn("带文件请求被上游拒绝（多半是该模型不支持文件输入）", err[:160])
    results["file"] = file_resp

    # ---- 带音频 ----
    # 豆包的音频同样走 base64 内联，与图片/文档是同一条 content parts 通道。
    # 测它是为了确认附件抽取对第三种 part 形态（input_audio）也认得出来
    reporter.info("发送带音频请求（WAV，base64 内联）…")
    audio_resp = await gw.chat(
        [{
            "role": "user",
            "content": [
                {"type": "text", "text": "这段音频有声音吗？"},
                {"type": "input_audio",
                 "input_audio": {
                     "data": base64.b64encode(_tiny_wav()).decode(),
                     "format": "wav",
                 }},
            ],
        }],
        model,
    )
    if "choices" in audio_resp:
        reporter.check(True, "带音频请求转发成功")
    else:
        err = (audio_resp.get("error") or {}).get("message", "")
        reporter.warn("带音频请求被上游拒绝（该模型可能不支持音频输入）", err[:160])
    results["audio"] = audio_resp

    # ---- 流式 ----
    reporter.info("发送流式请求（验证 include_usage 强制生效）…")
    stream_resp = await gw.chat(
        [{"role": "user", "content": "从 1 数到 5。"}], model, stream=True
    )
    reporter.check(bool(stream_resp.get("text")), "流式响应有内容")
    reporter.check(
        bool(stream_resp.get("usage")),
        "流式最后一个 chunk 带 usage",
        "拿不到的话网关会退化成按字符估算并打 tokens_estimated 标记",
    )
    results["stream"] = stream_resp

    # ---- 流式 + 中途断开 ----
    # 这一条专门盯着那个真实出现过的计费漏洞：记账写在最后一个 yield 之后时，
    # 客户端一断开生成器就被取消，这次调用的 token 完全不入库。
    # 报表上少一笔和正常毫无区别，所以必须由脚本逐笔核对。
    reporter.info("发送流式请求并中途断开（复现「停止生成」）…")
    aborted_resp = await gw.chat(
        [{"role": "user", "content": "写一段 300 字的公司简介。"}],
        model,
        stream=True,
        abort_after=2,
    )
    reporter.check(
        bool(aborted_resp.get("text")),
        "断开前已收到部分内容",
        f"{len(aborted_resp.get('text') or '')} 字",
    )
    reporter.info("→ 这次调用必须照样入库，并标记 aborted，后面会核对")
    results["aborted"] = aborted_resp

    # 返回**实际发出的请求数**：用量核对要拿它比对，少一笔就是漏计费
    return len(results), audit_sentinel


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="会话审计 + 附件 + 查阅日志 + 计费的端到端实测"
    )
    parser.add_argument("--base-url", default="http://localhost:7899", help="网关地址")
    parser.add_argument("--user", required=True, help="登录用户名（中文花名）")
    parser.add_argument("--password", required=True, help="登录密码")
    parser.add_argument(
        "--model",
        default="doubao-seed-2-0-lite-260428",
        help="目标模型，默认豆包多模态；传空则用列表里第一个",
    )
    args = parser.parse_args()

    reporter = Reporter()
    gw = Gateway(args.base_url, reporter)

    try:
        reporter.section("⓪ 登录与前置检查")
        user = await gw.login(args.user, args.password)
        reporter.info(
            f"登录成功：{user.get('username')} / 角色 {user.get('permission')} / "
            f"部门 {user.get('dept_name')}（{user.get('dept_code')}）"
        )

        # 审计可见性按部门判定（决策 #8、#17）：总裁无条件；
        # 其他人须「在管理部门」且「级别 ≥ 审计员」。判定规则与后端保持一致，
        # 不在管理部门的话后面的审计检查全是 403
        role = user.get("permission")
        can_audit = role == "president" or (
            user.get("dept_code") == "exec"
            and role in ("developer", "auditor", "admin")
        )
        if not can_audit:
            reporter.warn(
                f"账号「{user.get('username')}」（{role}）可能无权查阅会话内容",
                "总裁无条件；其他人须在管理部门且级别 ≥ 审计员",
            )
            reporter.info("→ 换总裁账号，或把该账号划入管理部门后重跑")

        model = await pick_model(gw, reporter, args.model or None)
        reporter.info(f"本次使用模型：{model}")

        # 先记下基线：库里本来就有历史数据，不减掉的话核对不了「这次记了几笔」
        try:
            baseline = (await gw.admin_get("/admin/usage/summary"))["total"]["requests"]
        except RuntimeError:
            baseline = 0

        sent, audit_sentinel = await send_requests(gw, reporter, model)

        # 写入是异步落库的，给它一点时间再回头查
        await asyncio.sleep(1.5)

        detail = await check_audit(gw, reporter, audit_sentinel)
        if detail:
            await check_attachment_download(gw, reporter, detail)
        await check_access_logs(gw, reporter, detail)
        await check_usage(gw, reporter, expected_requests=sent, baseline=baseline)
        await check_export(gw, reporter)

    except RuntimeError as exc:
        print(f"\n{FAIL} 接口调用失败：{exc}")
        reporter.fail(str(exc))
    finally:
        await gw.close()

    reporter.section("结论")
    if not reporter.ok:
        print(f"{FAIL} {len(reporter.failures)} 项未通过：")
        for item in reporter.failures:
            print(f"   · {item}")
        print("\n排查顺序建议：")
        print("  1. 迁移跑了吗 —— scripts/migrations/2026-08-08-conversation-audit.sql")
        print("  2. 账号在管理部门吗 —— 会话内容按部门授权，不按角色")
        print("  3. 附件目录可写吗 —— AUDIT_STORAGE_PATH")
        print("  4. 模型对该部门开通了吗 —— 白名单默认拒绝")
        return 1

    print(f"{PASS} 全部通过：转发 / 审计存储 / 附件落盘回读 / 查阅日志 / 用量计费")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
