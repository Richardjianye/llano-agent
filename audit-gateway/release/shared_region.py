"""取出「更新清单共享区」的正文，供两边测试比对

网关与客户端各有一份 canonical_manifest_bytes。两份代码之间没有 import 关系
（分属两个部署单元），唯一能挡住它们悄悄漂开的就是这个比对。

漂开的后果不是某台机器出错，而是**全量客户端同时验签失败**，所以宁可要一个
略显笨拙的「源码逐字比对」，也不要只靠固定向量——向量只钉住序列化那一段，
normalize / parse_version / manifest_applies_to 漂了它是看不出来的。
"""

from __future__ import annotations

import os

BEGIN = "# ==================== 共享区 开始 ===================="
END = "# ==================== 共享区 结束 ===================="

REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
GATEWAY_FILE = os.path.join(
    REPO_ROOT, "audit-gateway", "server", "services", "release_manifest.py"
)
CLIENT_FILE = os.path.join(REPO_ROOT, "hermes_cli", "update_manifest.py")


def shared_region(path: str) -> str:
    """标记之间的正文，去掉两边互指的那一行注释（它必然不同）。"""
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    if BEGIN not in source or END not in source:
        raise AssertionError(f"{path} 缺少共享区标记，改文件时不要删掉它们")
    body = source[source.index(BEGIN) + len(BEGIN): source.index(END)]
    lines = [ln for ln in body.splitlines() if not ln.startswith("# 以下内容必须与")]
    return "\n".join(lines).strip()
