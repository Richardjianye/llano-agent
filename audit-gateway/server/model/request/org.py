"""部门 / 模型 / 白名单管理的请求体"""

from decimal import Decimal
from typing import List, Optional

from model.database import ALL_SCOPES
from pydantic import BaseModel, Field, field_validator


# ==================== 部门 ====================

class DepartmentCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=64, description="部门编码，创建后不可改")
    name: str = Field(..., min_length=1, max_length=64)
    parent_id: Optional[int] = None
    remark: Optional[str] = Field(None, max_length=255)


class DepartmentUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=64)
    parent_id: Optional[int] = None
    status: Optional[int] = Field(None, ge=0, le=1)
    remark: Optional[str] = Field(None, max_length=255)
    dingtalk_dept_id: Optional[str] = Field(None, max_length=64)


# 调整用户部门/角色的请求体见 model/request/account.py（需要角色级别校验）


# ==================== 上游供应商 ====================

class ProviderCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=64, description="如 deepseek / partner-newapi")
    name: str = Field(..., min_length=1, max_length=64)
    base_url: str = Field(..., description="OpenAI 兼容的 base_url，如 https://api.deepseek.com")
    api_key: Optional[str] = Field(None, description="明文传入，服务端加密后入库")
    timeout_seconds: int = Field(60, ge=1, le=600)
    remark: Optional[str] = Field(None, max_length=255)


class ProviderUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=64)
    base_url: Optional[str] = None
    api_key: Optional[str] = Field(None, description="留空表示不修改")
    timeout_seconds: Optional[int] = Field(None, ge=1, le=600)
    status: Optional[int] = Field(None, ge=0, le=1)
    remark: Optional[str] = Field(None, max_length=255)


# ==================== 模型 ====================

# 客户端把「模型名」当成一个不带空格的标识符用：
# 桌面端切模型走 `/model <名字> --provider <槽位> --session`，命令行 `/model` 也一样
# 按空格分词。别名里一旦有空格，客户端就会把后半截当成参数，切换直接失败
# （2026-09-11 线上：别名填成「豆包 Seed2.0-Lite」，全员切不了模型）。
# 展示用的中文名请填 display_name，那一列不受此限制。
def _reject_whitespace(value: Optional[str], label: str, *, required: bool = False) -> Optional[str]:
    if value is None:
        if required:
            raise ValueError(f"{label}不能为空")
        return None
    trimmed = value.strip()
    if not trimmed:
        if required:
            raise ValueError(f"{label}不能为空")
        # 留空 = 不设别名，走上游真实模型名
        return None
    if any(ch.isspace() for ch in trimmed):
        raise ValueError(f"{label}不能包含空格，客户端按空格分词会切不了模型；中文展示名请填 display_name")
    return trimmed


class ModelCreate(BaseModel):
    provider_id: int
    model_name: str = Field(..., max_length=128, description="上游真实模型名")
    display_name: str = Field(..., max_length=128)
    alias: Optional[str] = Field(None, max_length=128, description="对外别名，留空则用 model_name")
    supports_stream: bool = True
    supports_multimodal: bool = False
    max_context_tokens: Optional[int] = Field(None, ge=1)
    billing_mode: str = Field("token", description="计价模式 token/per_call")
    # 单价用于报表估算费用；留空表示未配置，报表会把这部分标成「无法估算」而不是按 0 计
    input_price_per_1m: Optional[Decimal] = Field(None, ge=0, description="输入单价，元/百万 token")
    output_price_per_1m: Optional[Decimal] = Field(None, ge=0, description="输出单价，元/百万 token")
    # 留空 = 没单独配 → 按输入单价计。不是免费，别按 0 算
    cached_input_price_per_1m: Optional[Decimal] = Field(
        None, ge=0, description="缓存命中输入单价，元/百万 token；留空则按输入单价计"
    )
    price_per_call: Optional[Decimal] = Field(
        None, ge=0, description="按次单价，元/次；billing_mode=per_call 时生效"
    )
    currency: str = Field("CNY", max_length=8)
    remark: Optional[str] = Field(None, max_length=255)

    @field_validator("model_name")
    @classmethod
    def check_model_name(cls, v):
        return _reject_whitespace(v, "上游模型名", required=True)

    @field_validator("alias")
    @classmethod
    def check_alias(cls, v):
        return _reject_whitespace(v, "对外别名")


class ModelUpdate(BaseModel):
    display_name: Optional[str] = Field(None, max_length=128)
    alias: Optional[str] = Field(None, max_length=128)
    supports_stream: Optional[bool] = None
    supports_multimodal: Optional[bool] = None
    max_context_tokens: Optional[int] = Field(None, ge=1)
    billing_mode: Optional[str] = None
    # 显式传 null 表示「清空单价」，不传则不改动 —— 路由层靠 model_fields_set 区分
    input_price_per_1m: Optional[Decimal] = Field(None, ge=0)
    output_price_per_1m: Optional[Decimal] = Field(None, ge=0)
    cached_input_price_per_1m: Optional[Decimal] = Field(None, ge=0)
    price_per_call: Optional[Decimal] = Field(None, ge=0)
    currency: Optional[str] = Field(None, max_length=8)
    status: Optional[int] = Field(None, ge=0, le=1)
    remark: Optional[str] = Field(None, max_length=255)

    @field_validator("alias")
    @classmethod
    def check_alias(cls, v):
        return _reject_whitespace(v, "对外别名")


# ==================== 白名单 ====================

class WhitelistCreate(BaseModel):
    scope: str = Field(..., description="global / dept / role / user")
    scope_value: str = Field("", max_length=64, description="部门ID / 角色名 / 用户uid，global 留空")
    model_id: int
    allowed: bool = Field(True, description="False 为显式拒绝，优先级高于所有允许规则")
    remark: Optional[str] = Field(None, max_length=255)

    @field_validator("scope")
    @classmethod
    def check_scope(cls, v):
        if v not in ALL_SCOPES:
            raise ValueError(f"作用域非法，可选值：{', '.join(ALL_SCOPES)}")
        return v


class WhitelistTarget(BaseModel):
    scope: str = Field(..., description="global / dept / role / user")
    scope_value: str = Field("", max_length=64)

    @field_validator("scope")
    @classmethod
    def check_scope(cls, v):
        if v not in ALL_SCOPES:
            raise ValueError(f"作用域非法，可选值：{', '.join(ALL_SCOPES)}")
        return v


class WhitelistBatchCreate(BaseModel):
    """多个模型 × 多个对象一次授权。已存在的条目跳过、不覆盖。"""
    model_ids: List[int] = Field(..., min_length=1, max_length=100)
    targets: List[WhitelistTarget] = Field(..., min_length=1, max_length=1000)
    allowed: bool = Field(True, description="False 为显式拒绝，优先级高于所有允许规则")
    remark: Optional[str] = Field(None, max_length=255)


class WhitelistBatchDelete(BaseModel):
    ids: List[int] = Field(..., min_length=1, max_length=1000)


class WhitelistUpdate(BaseModel):
    allowed: Optional[bool] = None
    remark: Optional[str] = Field(None, max_length=255)
