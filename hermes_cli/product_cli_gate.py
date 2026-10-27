"""品牌构建里 CLI 子命令的能力闸门

## 为什么单独有这一层

能力策略原本只在四个地方执行：配置写入、仪表盘 REST、TUI RPC、slash 命令
（`hermes_cli/commands.py` 的 `COMMAND_CAPABILITIES`）。**`hermes xxx` 这条
命令行入口一直没人把守。**

在我们的产品里这不是理论问题：桌面端自带终端工具，`/moa` 在命令面板里是灰的，
但同一台机器上敲 `hermes moa configure` 直接就进去了。`hermes update` 更直接 ——
它会从 NousResearch 的官方仓库 git pull，把整棵源码树换掉。

所以这里把 slash 命令那套「命令名 → 能力位」的做法原样搬到 CLI 上，
挂在 `main.py` 唯一的 `args.func(args)` 分发点上，而不是往每个子命令里塞判断：
分发点只有一个，往子命令里塞就变成十几个各自演化的判断。

## 只按顶层子命令判定

`hermes moa configure` 与 `hermes moa list` 一起挡掉。不做「读操作放行、
写操作拒绝」的细分 —— 那需要为每个子命令维护一张读写表，而表一旦漏一格，
漏的那格就是个洞。整条命令挡掉，代价只是少了几个查看类命令，
而它们在仪表盘里都有对应入口。
"""

from __future__ import annotations

from typing import Optional

# 顶层子命令 → 需要的能力位。没列进来的命令不受能力策略管辖。
#
# 与 `hermes_cli/commands.py` 的 `COMMAND_CAPABILITIES`（slash 命令）是两张表，
# 因为两者的命令名空间本来就不一样：slash 有 `/subscription`、`/topup`，
# CLI 这边它们都在 `hermes portal` 底下。
CLI_COMMAND_CAPABILITIES: dict[str, str] = {
    # 自有更新闭环之后，官方 git/ZIP 更新通道必须彻底不可达
    "update": "self_update",
    "moa": "moa",
    # fallback 链会把请求指向别的 provider，属于供应商管理
    "fallback": "provider_management",
    # Nous 账号 / 订阅 / 充值
    "portal": "nous_services",
}

# 每条被拒命令给一句「那该怎么办」。只说拒绝不给去处，用户下一步就是找我们问。
_ALTERNATIVES: dict[str, str] = {
    "update": "版本更新由公司统一下发，请在应用内检查更新。",
    "moa": "多模型编排在本产品中未启用。",
    "fallback": "模型与降级链由公司网关统一配置，请联系管理员。",
    "portal": "本产品不使用 Nous 账号体系，模型额度由公司网关管理。",
}


def cli_command_capability(name: str) -> Optional[str]:
    """该子命令需要的能力位；不受管辖时返回 None。"""
    return CLI_COMMAND_CAPABILITIES.get(str(name or "").strip().lower())


def cli_command_allowed(name: str) -> bool:
    """本构建是否允许执行该 CLI 子命令。stock Hermes 一律允许。"""
    from hermes_cli.product_policy import capability_allowed

    capability = cli_command_capability(name)
    return capability is None or capability_allowed(capability)


def cli_command_denial_message(name: str) -> str:
    normalized = str(name or "").strip().lower()
    alternative = _ALTERNATIVES.get(normalized, "")
    lines = [f"`hermes {normalized}` 在本产品中不可用。"]
    if alternative:
        lines.append(alternative)
    return "\n".join(lines)
