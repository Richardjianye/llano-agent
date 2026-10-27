"""品牌 sidecar 的冻结入口。

桌面端拉起来的是 `hermes serve --host 127.0.0.1 --port 0`，所以这里就是
`hermes_cli.main.main()` 的一层薄壳，args 原样透传。

两处刻意的写法，都是实测踩出来的（见 TODO.md「S6-E 打包选型实测」）：

1. `import hermes_cli.main` **必须在模块顶层**。放进 `if __name__` 里
   PyInstaller 的静态分析一个模块都收不到 —— 产物 20 MB、2 秒打完、构建
   报成功，一跑就 ModuleNotFoundError。
2. `--llano-probe` 是出厂自检用的。静态分析会**静默**漏掉只在函数体里 import
   的依赖（实测漏了 jinja2 / tenacity / tqdm 三个核心依赖），而漏了之后
   `serve` 照样启动、照样打印 HERMES_BACKEND_READY，失败被推迟到用户点到
   那个功能的那一刻。构建日志里没有任何线索，只能靠对着冻结产物逐个 import。
"""
import multiprocessing
import sys

# Keep the import visible to PyInstaller's static analysis. The internal
# supervisor must launch without booting the Hermes CLI or writing on stdio.
if sys.argv[1:2] != ["--llano-mcp-watchdog"]:
    import hermes_cli.main


def _probe(names: str) -> int:
    """逐个 import 并报告，供 scripts/build-sidecar.mjs 在构建末尾核对。

    `dist:<名字>` 查的是包元数据（importlib.metadata），对应 --copy-metadata。
    """
    import importlib
    import importlib.metadata

    missing = []
    for name in [n.strip() for n in names.split(",") if n.strip()]:
        try:
            if name.startswith("dist:"):
                importlib.metadata.version(name[len("dist:"):])
            else:
                importlib.import_module(name)
        except Exception as exc:
            missing.append(f"{name} ({type(exc).__name__})")

    if missing:
        print("LLANO_PROBE_MISSING=" + ";".join(missing))
        return 1

    print("LLANO_PROBE_OK")
    return 0


def _terminal_probe() -> int:
    """Exercise the actual frozen terminal backend in an isolated writable home."""
    import json
    import os
    import tempfile
    from tools.environments.local import LocalEnvironment

    with tempfile.TemporaryDirectory(prefix="llano-terminal-check-") as home:
        os.environ["HERMES_HOME"] = home
        terminal = LocalEnvironment(cwd=home, timeout=60)
        try:
            result = terminal.execute(
                "printf 'llano-terminal-ok\\n' && git --version && "
                "python -c 'print(6 * 7)' && node --version && rg --version && ffmpeg -version",
                timeout=60,
            )
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result.get("returncode") == 0 and "llano-terminal-ok" in result.get("output", "") else 1
        finally:
            terminal.cleanup()


def _tool_probe() -> int:
    """Inspect cold-start schema resolution without credentials or model calls."""
    import json
    import model_tools
    from tools.registry import registry
    from tui_gateway.server import _load_enabled_toolsets
    from agent.conversation_mode import configured_chat_tools
    from agent.web_search_registry import get_active_search_provider
    enabled = _load_enabled_toolsets('desktop')
    allowed = configured_chat_tools(enabled)
    provider = get_active_search_provider()
    schemas = model_tools.get_tool_definitions(
        enabled_toolsets=['web'] if allowed else [], quiet_mode=True,
        skip_tool_search_assembly=True)
    print('LLANO_TOOL_PROBE=' + json.dumps({
        'enabled_toolsets': enabled,
        'chat_allowed_tools': sorted(allowed),
        'web_provider_available': bool(provider and provider.is_available()),
        'registered_tools': sorted(registry._tools),
        'chat_schema': sorted(t['function']['name'] for t in schemas if t['function']['name'] in allowed)
    }))
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()

    if sys.argv[1:2] == ["--llano-mcp-watchdog"]:
        from tools.mcp_stdio_watchdog import main as watchdog_main
        sys.exit(watchdog_main(sys.argv[2:]))

    if len(sys.argv) == 3 and sys.argv[1] == "--llano-probe":
        sys.exit(_probe(sys.argv[2]))

    if sys.argv[1:] == ["--llano-terminal-probe"]:
        sys.exit(_terminal_probe())

    if sys.argv[1:] == ["--llano-tool-probe"]:
        sys.exit(_tool_probe())

    sys.exit(hermes_cli.main.main())
