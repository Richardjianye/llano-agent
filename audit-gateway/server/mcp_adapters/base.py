from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class Capability:
    code: str
    name: str
    domain: str
    method: str
    path: str
    input_schema: dict[str, Any]
    source_hash: str
    metadata: dict[str, Any]


class McpAdapter(ABC):
    key: str
    display_name: str
    description: str
    permission_mode = "local_grants"
    credential_platform: str | None = None

    @abstractmethod
    def config_schema(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def capabilities(self) -> tuple[Capability, ...]: ...

    @abstractmethod
    def validate_config(self, auth: Mapping[str, Any], config: Mapping[str, Any]) -> None: ...

    @abstractmethod
    async def execute(
        self,
        capability: Capability,
        arguments: Mapping[str, Any],
        auth: Mapping[str, Any],
        config: Mapping[str, Any],
    ) -> Any: ...

    @abstractmethod
    async def health(self, auth: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]: ...
