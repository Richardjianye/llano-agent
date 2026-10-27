"""组织架构初始数据

首次建表时幂等写入。改这里只影响**新建**的部门，已存在的（按 code 判断）不会被覆盖，
避免后台改过的名称被一次重启打回原样。

部门编码一旦用于白名单配置就不要再改 —— 白名单存的是部门 ID，
但编码是人读的稳定标识，改名会让运维对不上账。
"""

from typing import List, Tuple

# (编码, 名称)
DEFAULT_DEPARTMENTS: List[Tuple[str, str]] = [
    ("exec", "管理部门"),
    ("demo", "示例部门"),
]

# 初始管理员账号；密码由部署流程生成
PRESIDENT_USERNAME = "admin"
PRESIDENT_REALNAME = "admin"
PRESIDENT_NICKNAME = "admin"
PRESIDENT_DEPT_CODE = "exec"

# 管理部门的部门编码。会话审计（audit:view）现在按**角色**授权
# （总裁 / 开发者 / 管理部门审计），不再依赖它；提示词策略页里
# 「管理部门负责人」等按部门判定的规则仍以它为锚点。
AUDIT_DEPT_CODE = "exec"
