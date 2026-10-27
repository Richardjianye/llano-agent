"""管理员确认的同步动作，所有身份字段由服务端快照重新读取。"""
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class DirectoryAction(BaseModel):
    key: str = Field(min_length=1, max_length=160)
    action: Literal["bind", "create", "disable"]
    uid: str | None = Field(default=None, max_length=36)
    username: str | None = Field(default=None, min_length=1, max_length=36)
    dept_id: int | None = None

    @field_validator("username")
    @classmethod
    def valid_username(cls, value):
        if value is not None:
            value = value.strip()
            if not value or any(c.isspace() for c in value):
                raise ValueError("用户名不能为空或含空格")
        return value


class DirectoryApply(BaseModel):
    snapshot_id: str = Field(min_length=1, max_length=36)
    actions: list[DirectoryAction] = Field(min_length=1, max_length=100)


class DirectoryExport(BaseModel):
    snapshot_id: str = Field(min_length=1, max_length=36)
    user_uids: list[str] = Field(min_length=1, max_length=100)
