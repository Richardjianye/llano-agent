#!/usr/bin/env python3
"""Read which OS window sits directly underneath the Hermes desktop window.

The window list lives with the OS, so this tool round-trips through the
gateway's blocking-prompt bridge — the same one `read_terminal` uses:
tui_gateway emits ``window.read.request``, the desktop renderer asks its main
process (which owns native window enumeration) and answers with
``window.read.respond``. This module is just schema + a thin dispatcher over
the platform-injected callback.
"""

import json
from typing import Callable, Optional

from tools.registry import registry, tool_error


def read_window_below_tool(callback: Optional[Callable] = None) -> str:
    """Return the window underneath the Hermes window as a JSON string."""
    # 品牌构建关掉了窗口枚举（见 product_policy.WINDOW_ENUMERATION）。
    # 在这里挡而不是让它自然失败，是为了把原因说清楚：底层依赖被拿掉之后，
    # 自然失败给出的是「桌面端没有应答」—— 那句话会让人去查 IPC，
    # 而真正的原因是这个能力被产品策略关了。
    try:
        from hermes_cli.product_policy import WINDOW_ENUMERATION, capability_allowed

        if not capability_allowed(WINDOW_ENUMERATION):
            return tool_error(
                "read_window_below is disabled in this build: enumerating other "
                "applications' window titles is not available in the branded product."
            )
    except ImportError:
        # 原版 Hermes 没有产品策略这一层，照常往下走。
        pass

    if callback is None:
        return tool_error(
            "read_window_below is only available in the Hermes desktop app."
        )

    try:
        raw = callback()
    except Exception as exc:
        return tool_error(f"Failed to read the window below: {exc}")

    if not raw:
        return tool_error(
            "Could not determine the window underneath (the desktop app did "
            "not answer, or window enumeration is unavailable on this system)."
        )

    # Desktop answers with a JSON object; pass it through, else wrap the raw text.
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps({"text": str(raw)}, ensure_ascii=False)


READ_WINDOW_BELOW_SCHEMA = {
    "name": "read_window_below",
    "description": (
        "Identify the application window directly underneath (behind) the "
        "Hermes desktop window — what the user is working in behind this app. "
        "Returns JSON: {window: {app, title, bounds{x,y,width,height}, id}, "
        "frontmost: {app, title}, platform}. `title` may be empty when the OS "
        "withholds window titles (e.g. macOS without the Screen Recording "
        "permission — never prompted for, noted in `note`). Other Hermes "
        "windows are skipped: the nearest non-Hermes window is reported. "
        "Returns {error, platform} instead where the OS cannot enumerate "
        "windows at all (e.g. a Wayland session); `error` says what would fix "
        "it, so relay it rather than retrying. "
        "Metadata only; this never captures pixels or content of other windows."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}


registry.register(
    name="read_window_below",
    toolset="desktop_ui",
    schema=READ_WINDOW_BELOW_SCHEMA,
    handler=lambda args, **kw: read_window_below_tool(callback=kw.get("callback")),
    emoji="🪟",
)
