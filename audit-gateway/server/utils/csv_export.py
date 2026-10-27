"""CSV 导出安全工具。"""

from __future__ import annotations

from typing import Any

_FORMULA_PREFIXES = ("=", "+", "-", "@")


def safe_csv_cell(value: Any) -> Any:
    """阻止 Excel/表格软件把用户可控文本当作公式执行。"""
    if not isinstance(value, str) or not value:
        return value

    visible = value.lstrip(" \t\r\n")
    if visible.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value
