from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator

# ============================================
# 统一错误码映射
# ============================================
code_map = {
    -1: "未知错误",
    0: "请求成功",
    10001: "用户名或密码错误",
    10002: "身份凭证失效，请重新登录(AT)",
    10003: "身份凭证失效，请重新登录(RT)",
    10004: "字段长度超过阈值",
    10005: "名称已存在",
    10006: "缺少关键字段",
    10007: "账号已停用，如有疑问请联系管理员",
    10008: "账户无权限",
    10009: "操作无效",
    10010: "数据错误",
    # GET /v1/prompt-policy/effective 的空库状态。客户端遇到它必须使用本次
    # 安装包内置基线，不能把旧 LKG 当成网关当前发布版。
    10011: "提示词策略尚未发布",
    10012: "请先修改初始密码",
}


class Response(BaseModel):
    """统一响应模型

    ## code 说明
    - 0: 请求成功
    - -1: 未知错误
    - 10001: 用户名或密码错误
    - 10002: 身份凭证失效 (AT)
    - 10003: 身份凭证失效 (RT)
    - 10004 起: 业务错误
    """
    code: int = 0
    message: str = ""
    data: Any = {}

    @model_validator(mode="after")
    def set_message_from_code(self):
        """code 修改时自动映射到 message"""
        if self.message == "" and self.code in code_map:
            self.message = code_map[self.code]
        return self


class UserInfo(BaseModel):
    """用户信息模型"""
    id: int
    uid: str = ""
    username: Optional[str] = None
    nickname: Optional[str] = None
    realname: Optional[str] = None
    status: int = 1
    permission: str
    dept_id: Optional[int] = None
    dept_name: Optional[str] = None
    # 会话审计可见性按部门判定，所以登录态里要带上部门编码
    dept_code: Optional[str] = None
    expired: Optional[int] = None
    permissions: list[str] = Field(default_factory=list)
    default_route: str = "dashboard"
    must_change_password: bool = False


class LoginInfo(BaseModel):
    """登录信息模型（令牌与当前用户授权上下文一次下发）"""
    token: Optional[str] = ""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserInfo

    @model_validator(mode="after")
    def set_token_from_access_token(self):
        self.token = self.access_token
        return self


class DepartmentInfo(BaseModel):
    """部门信息模型"""
    id: int
    code: str
    name: str
    dingtalk_dept_id: Optional[str] = None
    parent_id: Optional[int] = None
    status: int = 1
    remark: Optional[str] = None


class ModelPublicInfo(BaseModel):
    """下发给客户端的模型信息（不含上游地址与密钥）"""
    id: str
    display_name: str
    supports_stream: bool = True
    supports_multimodal: bool = False
    max_context_tokens: Optional[int] = None


class ModelAdminInfo(BaseModel):
    """管理端模型信息"""
    id: int
    provider_id: int
    provider_code: Optional[str] = None
    capability: str = "chat"
    model_name: str
    display_name: str
    alias: Optional[str] = None
    supports_stream: bool = True
    supports_multimodal: bool = False
    max_context_tokens: Optional[int] = None
    # 计价模式：token（按 token）/ per_call（按次）
    billing_mode: str = "token"
    # 单价 None 表示未配置：报表遇到它会把费用标成「无法估算」，而不是当免费
    input_price_per_1m: Optional[float] = None
    output_price_per_1m: Optional[float] = None
    # 留空 = 没单独配，计算时回退到输入单价（不是免费）
    cached_input_price_per_1m: Optional[float] = None
    price_per_call: Optional[float] = None
    currency: str = "CNY"
    status: int = 1
    remark: Optional[str] = None


class ProviderInfo(BaseModel):
    """上游供应商信息，api_key 一律脱敏后返回"""
    id: int
    code: str
    name: str
    base_url: str
    kind: str = "openai"
    api_key_masked: str = ""
    timeout_seconds: int = 60
    status: int = 1
    remark: Optional[str] = None


class WhitelistEntryInfo(BaseModel):
    """白名单条目"""
    id: int
    scope: str
    scope_value: str
    model_id: int
    allowed: bool = True
    remark: Optional[str] = None


# ==================== 用量统计 ====================


class UsageMetrics(BaseModel):
    """一组指标 —— 概览、分组行、趋势点共用同一套字段，前端只写一份渲染逻辑

    口径提示：
    - token 数来自上游返回的 usage，与账单可能有出入
    - estimated_* 是上游没返回 usage 时按字符估算的部分，必须与实测分开看
    - cost 为 None 表示这组里存在未配置单价的模型，费用算不全，别显示成 0
    """
    requests: int = 0
    success_count: int = 0
    failed_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_requests: int = 0
    estimated_tokens: int = 0
    avg_latency_ms: int = 0
    cost: Optional[float] = None
    currency: str = "CNY"


class UsageGroupRow(BaseModel):
    """按某个维度分组后的一行"""
    key: str
    label: str
    metrics: UsageMetrics


class UsageTrendPoint(BaseModel):
    """趋势图上的一个时间桶"""
    bucket: str
    label: str
    metrics: UsageMetrics


class UsageRecordInfo(BaseModel):
    """用量明细的一行（来自 usage_records，不含聊天内容）"""
    id: int
    request_id: str
    created_at: Optional[str] = None
    user_uid: str
    username: Optional[str] = None
    dept_id: Optional[int] = None
    dept_name: Optional[str] = None
    role: Optional[str] = None
    model_name: Optional[str] = None
    stream: bool = False
    status: str = "success"
    error_code: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    tokens_estimated: bool = False
    latency_ms: int = 0
