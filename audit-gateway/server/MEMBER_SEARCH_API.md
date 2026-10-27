# 成员搜索接口契约

2026-10-19 起，管理后台所有「选人」控件（模型白名单、API Key、MCP 授权、配额）统一走这个接口。
以前它们一次拉 `/admin/account/list?limit=200`（配额页 500）再在下拉框里本地过滤。生产有 290 个账号，
按 ID 排在第 200 名以后的人在候选里根本不存在，搜不到也不报错。

实现：`router/admin/member.py` → `services/member_search.py`。测试：`tests/test_model_grants.py`。

## 1. 管理端接口（已实现）

```
GET /admin/member/search
Authorization: Bearer <管理端 access_token>   （或 httpOnly Cookie）
```

权限点：`member:search`（目前开放给总裁、开发者）。它是单独的权限点，没有借用 `user:view`：
选人只需要姓名、账号、部门，不该连带看到账号管理页的全部字段。

### 参数

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `keyword` | string ≤64 | 空 | 姓名、账号、昵称做**包含**匹配；uid、钉钉 userId 做**精确**匹配。`%` `_` `\` 按字面处理 |
| `dept_ids` | 逗号分隔整数 | 空 | 按部门过滤 |
| `include_descendants` | bool | `true` | 部门过滤是否包含全部下级部门 |
| `roles` | 逗号分隔 | 空 | `president,developer,auditor,manager,employee` |
| `status` | `active` / `disabled` / `all` | `active` | 默认只返回在职账号 |
| `uids` | 逗号分隔，最多 200 个 | 空 | 按 uid 精确取回，用于回显已选中的人 |
| `limit` | 1–100 | 20 | 每页条数 |
| `offset` | ≥0 | 0 | 偏移 |

排序：姓名或账号与关键字完全相等的排前面，其余按账号 ID 升序。翻页结果稳定。

### 响应

```json
{
  "code": 0,
  "data": {
    "items": [
      {
        "uid": "ad7b…",
        "username": "源贺",
        "realname": "源贺",
        "nickname": "源贺",
        "display_name": "源贺",
        "role": "manager",
        "status": 1,
        "dept_id": 30,
        "dept_name": "设计创意部",
        "dept_path": "视觉中心 / 设计创意部"
      }
    ],
    "total": 1,
    "limit": 20,
    "offset": 0,
    "has_more": false
  }
}
```

**不返回**的字段：密码哈希、最后登录时间、钉钉 userId、账号类型、`manageable`。新增字段要先确认是否需要
扩大暴露面，不能因为「顺手」就加。

### 错误

与网关其他管理接口使用同一个信封：参数非法时返回 `code=10006`，无权限时返回 `code=10008`。
HTTP 状态码统一为 200，以信封里的 code 为准。

## 2. 对外接口：第三方平台（OSS 等）（已实现）

第三方平台会在自己那边维护一套权限表，**以网关的用户 ID 和昵称为准**。所以对外只回这两个
标识，外加在职状态：

- `uid`：网关用户 ID。改名、改昵称、调部门、改账号名都**不变**，第三方应该用它做主键
- `nickname`：网关昵称。可能被管理员修改，第三方按 uid 定期回查同步
- `status`：`active` / `disabled`，用来同步离职和停用

不回账号名、角色、部门、钉钉 ID。

实现：`router/open_api.py` → `services/open_directory.py`。测试：`tests/test_open_directory.py`。

### 2.1 接入应用登记

复用企业统一登录的 `sso_clients`（总裁在后台登记）。能调用的应用必须同时满足：

- `enabled = 1`，`environment` 与网关环境一致（prod 库只认 prod 登记的应用）
- 有 `client_secret`（机密应用）
- `allowed_scopes` 里含 `directory.read`

`directory.read` 只能通过下面的 client_credentials 流程拿到，不能通过 OIDC 授权码流程拿到。

### 2.2 换令牌

```
POST /open/v1/token
Content-Type: application/x-www-form-urlencoded
Authorization: Basic base64(client_id:client_secret)      # 或在表单里带 client_id / client_secret

grant_type=client_credentials&scope=directory.read
```

```json
{"access_token": "…", "token_type": "Bearer", "expires_in": 600, "scope": "directory.read"}
```

令牌是不透明字符串，存在 Redis 里，有效期 10 分钟，到期重新换。

### 2.3 搜索

```
GET /open/v1/members/search?keyword=源贺&limit=20&offset=0
Authorization: Bearer <access_token>
```

| 参数 | 说明 |
|---|---|
| `keyword` | 姓名、账号、昵称包含匹配，uid 精确匹配 |
| `uids` | 逗号分隔，最多 100 个，按 uid 批量回查（同步昵称和在职状态用） |
| `status` | `active`（默认）或 `all`（包含已停用） |
| `limit` / `offset` | 每页 1–50 条 |

`keyword` 和 `uids` **至少给一个**：不带条件就等于一页一页导出全员名册。

```json
{
  "items": [{"uid": "ad7b…", "nickname": "源贺", "status": "active"}],
  "total": 1, "limit": 20, "offset": 0, "has_more": false
}
```

### 2.4 错误、限流、审计

- 错误按 OAuth 2.0 格式返回：`{"error": "...", "error_description": "..."}`，状态码 400/401/403/429/503。
  凭据错误、应用不存在、应用已停用、环境不符，一律返回同一个 `invalid_client`。
- 限流：每个应用每分钟 `OPEN_API_RATE_LIMIT_PER_MINUTE` 次（默认 120），换令牌和搜索合并计数。
- 审计：每次调用写一行 `open_api_call_logs`，记录应用、接口、结果、条数、来源 IP、查询摘要和查询指纹，
  **不存关键字原文**。成功返回数据之前必须先写入审计，写不进去就返回 503，不给数据。
- `sso_clients` 表还没建时（企业统一登录尚未上线），所有应用都返回 `invalid_client`，不影响网关其他功能。
