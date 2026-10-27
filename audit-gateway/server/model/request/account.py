"""账号管理请求体"""

from typing import Optional

from model.database import ASSIGNABLE_ROLES
from pydantic import BaseModel, Field, field_validator


def _check_role(value: Optional[str]) -> Optional[str]:
    if value is not None and value not in ASSIGNABLE_ROLES:
        raise ValueError(f"角色非法，可选值：{', '.join(ASSIGNABLE_ROLES)}")
    return value


class AccountCreate(BaseModel):
    """新建账号。用户名用中文花名（如「admin」），不强制英文"""

    username: str = Field(..., min_length=1, max_length=36, description="登录用户名，中文花名")
    permission: str = Field(..., description="角色")
    realname: Optional[str] = Field(None, max_length=36, description="真实姓名")
    nickname: Optional[str] = Field(None, max_length=36)
    dept_id: Optional[int] = None
    password: Optional[str] = Field(
        None, min_length=8, max_length=64, description="留空则随机生成并在响应中返回一次"
    )
    status: int = Field(1, ge=0, le=1)

    @field_validator("username")
    @classmethod
    def strip_username(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("用户名不能为空")
        if any(ch.isspace() for ch in v):
            raise ValueError("用户名不能包含空格")
        return v

    @field_validator("permission")
    @classmethod
    def check_permission(cls, v: str) -> str:
        return _check_role(v)


class AccountAssign(BaseModel):
    """调整账号的部门 / 角色"""

    uid: str
    dept_id: Optional[int] = None
    permission: Optional[str] = None
    realname: Optional[str] = Field(None, max_length=36)
    nickname: Optional[str] = Field(None, max_length=36)

    @field_validator("permission")
    @classmethod
    def check_permission(cls, v: Optional[str]) -> Optional[str]:
        return _check_role(v)


class AccountStatus(BaseModel):
    """启用 / 禁用账号"""

    uid: str
    status: int = Field(..., ge=0, le=1, description="1 启用 0 禁用")


class AccountPasswordReset(BaseModel):
    """重置密码。留空则随机生成并返回一次"""

    uid: str
    password: Optional[str] = Field(None, min_length=8, max_length=64)


class SelfPasswordChange(BaseModel):
    """自助修改密码

    ⚠️ 必须带 current_password —— 这是「修改」与「重置」的全部区别。

    不要它的话，任何拿到一个有效会话的人（偷到 Cookie、借用了没锁屏的电脑、
    XSS）都能把密码改掉，把真正的机主锁在门外 —— **一次会话劫持就升级成
    永久接管**，而且受害者连「我的密码怎么不对了」之外的线索都没有。

    带上它，攻击者顶多在会话有效期内为所欲为，机主改一次密码就把他踢出去了。
    这是「会话被偷」和「账号丢了」之间的那道坎。
    """

    current_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=64)
    # 怀疑凭据外泄时勾选。默认不动 API Key：它们是用户自己另外签发的凭据，
    # 例行改密就把桌面客户端全部踢下线，只会让人不愿意改密码。
    revoke_api_keys: bool = False
