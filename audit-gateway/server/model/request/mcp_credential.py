from pydantic import BaseModel, ConfigDict, Field


class McpCredentialBind(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)
