"""E2E 检查结果的收集与输出。

原先 `check` / `warn` / `info` / `section` 是模块级函数，失败计数存在模块级
`_failures` 列表里。抽成类是为了让**同一进程里能跑多轮互不干扰的对账** ——
阶段 4 的客户端链路测试要在一次运行里分别核对「登录后」「切换模型后」「退出后」
几段，用全局计数的话第二段会带着第一段的失败一起报。

`to_dict()` 给的是机器可读的结果，供 Playwright 那边解析；人类可读的输出
仍然照常打印，两者内容一致。
"""

from __future__ import annotations

from typing import Any, Dict, List

PASS, FAIL, WARN, INFO = "✅", "❌", "⚠️ ", "  "


class Reporter:
    """一轮对账的结果收集器。

    `quiet=True` 时不打印，只收集 —— 被别的程序调用时用得上，
    免得把人类可读的排版混进它要解析的输出里。
    """

    def __init__(self, *, quiet: bool = False) -> None:
        self.quiet = quiet
        self.failures: List[str] = []
        self.warnings: List[str] = []
        self.checks: List[Dict[str, Any]] = []

    def check(self, ok: bool, title: str, detail: str = "") -> bool:
        self._print(f"{PASS if ok else FAIL} {title}" + (f" —— {detail}" if detail else ""))
        self.checks.append({"ok": bool(ok), "title": title, "detail": detail})
        if not ok:
            self.failures.append(title)
        return ok

    def warn(self, title: str, detail: str = "") -> None:
        self._print(f"{WARN}{title}" + (f" —— {detail}" if detail else ""))
        self.warnings.append(title)

    def info(self, text: str) -> None:
        self._print(f"{INFO}{text}")

    def section(self, title: str) -> None:
        self._print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")

    def fail(self, title: str) -> None:
        """记一条不来自 check() 的失败（例如接口整个调不通）。"""
        self.failures.append(title)

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "failures": list(self.failures),
            "warnings": list(self.warnings),
            "checks": list(self.checks),
        }

    def _print(self, text: str) -> None:
        if not self.quiet:
            print(text)
