"""MCP 管理与调用请求体。"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class McpInstanceCreate(BaseModel):
    code: str = Field(..., min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(..., min_length=1, max_length=128)
    adapter_key: str = Field(..., min_length=1, max_length=64)
    auth_config: Dict[str, Any] = Field(default_factory=dict)
    config: Dict[str, Any] = Field(default_factory=dict)
    remark: Optional[str] = Field(None, max_length=255)


class McpInstanceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(None, min_length=1, max_length=128)
    auth_config: Optional[Dict[str, Any]] = None
    config: Optional[Dict[str, Any]] = None
    status: Optional[int] = Field(None, ge=0, le=1)
    remark: Optional[str] = Field(None, max_length=255)


class McpPermissionBatch(BaseModel):
    user_uid: str = Field(..., min_length=1, max_length=36)
    capability_codes: List[str] = Field(default_factory=list, max_length=1000)
    effect: str = "allow"
    constraints: Dict[str, Any] = Field(default_factory=dict)
    expires_at: Optional[datetime] = None
    remark: Optional[str] = Field(None, max_length=255)
    replace: bool = True

    @field_validator("effect")
    @classmethod
    def check_effect(cls, value: str) -> str:
        if value not in {"allow", "deny"}:
            raise ValueError("effect 只能为 allow 或 deny")
        return value


class McpExecuteRequest(BaseModel):
    capability_code: str = Field(..., min_length=3, max_length=128)
    instance_code: Optional[str] = Field(None, max_length=64)
    arguments: Dict[str, Any] = Field(default_factory=dict)


class McpSearchRequest(BaseModel):
    query: Optional[str] = Field(None, max_length=128)
    instance_code: Optional[str] = Field(None, max_length=64)
    domain: Optional[str] = Field(None, max_length=64)
    limit: int = Field(20, ge=1, le=100)
