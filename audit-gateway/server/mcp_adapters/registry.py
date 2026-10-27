from __future__ import annotations

from functools import lru_cache

from mcp_adapters.base import McpAdapter
from mcp_adapters.internal_datahub import InternalDatahubAdapter
from mcp_adapters.lingxing import LingxingAdapter
from utils.errors import ValidationError_


@lru_cache
def _registry() -> dict[str, McpAdapter]:
    adapters: tuple[McpAdapter, ...] = (LingxingAdapter(), InternalDatahubAdapter())
    return {adapter.key: adapter for adapter in adapters}


def get_adapter(key: str) -> McpAdapter:
    adapter = _registry().get(key)
    if adapter is None:
        raise ValidationError_(f"未安装或不受信任的 MCP 适配器：{key}")
    return adapter


def list_adapters() -> list[dict]:
    return [
        {
            "key": adapter.key,
            "name": adapter.display_name,
            "description": adapter.description,
            "permission_mode": adapter.permission_mode,
            "credential_platform": adapter.credential_platform,
            "config_schema": adapter.config_schema(),
            "capability_count": len(adapter.capabilities()),
        }
        for adapter in _registry().values()
    ]
