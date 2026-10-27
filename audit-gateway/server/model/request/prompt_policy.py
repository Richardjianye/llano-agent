"""提示词策略管理请求。正文限制由 service 按字符数与 UTF-8 总字节校验。"""

from pydantic import BaseModel, Field


class PromptPolicyDraftSave(BaseModel):
    company_core_policy: str
    default_persona: str
    expected_revision: int = Field(ge=0)


class PromptPolicyPublish(BaseModel):
    expected_revision: int = Field(ge=0)


class PromptPolicyRollback(BaseModel):
    expected_current_version: int = Field(ge=1)
