# audit-gateway / server

LLANO Agent的自研网关（FastAPI）。当前完成度：**账号角色部门体系 + 模型白名单 +
上游转发 + 会话审计存储 + 脱敏兜底 + 用量统计（写入与报表读取）**。
尚未实现的见文末「已知待办」。

设计约束来自根目录 `TODO.md` 的已确认决策，改动前请先读那份文档。

## 项目结构

```
server/
├── config.py             # Pydantic Settings 配置（含加密密钥、CORS）
├── main.py               # 应用工厂 + 启动自检
├── pytest.ini
├── .env.example          # 环境变量模板
├── db/                   # 数据访问
│   ├── __init__.py       # 异步引擎、建表、初始管理员
│   ├── selects/          # user / department / catalog
│   ├── inserts/
│   └── updates/
├── model/
│   ├── database.py       # ORM 模型 + 角色/作用域常量
│   ├── response.py       # 统一响应与错误码
│   └── request/          # 请求体（user.py / org.py）
├── router/
│   ├── health.py         # /healthz /readyz
│   ├── admin/            # user / department / catalog
│   └── client/           # 客户端模型清单
├── services/
│   ├── model_access.py   # 模型白名单判定（纯逻辑，可单测）
│   ├── permissions.py    # 权限真源：功能权限 / 审计可见性 / 用量数据范围
│   ├── usage.py          # 用量与会话的**写入**
│   └── usage_query.py    # 用量报表的**读取**（分桶、聚合、费用估算，纯逻辑）
├── utils/
│   ├── crypt.py          # bcrypt 哈希 + JWT（本系统密码，不可逆）
│   ├── secret_box.py     # Fernet 可逆加密（中台凭证 / 上游 Key）
│   ├── dependencies.py   # 认证与 RBAC 依赖项
│   ├── errors.py         # 业务异常
│   ├── middleware.py     # request_id、访问日志、全局异常处理
│   ├── cache.py          # Redis 连接池
│   └── token_manager.py  # Token 黑/白名单
├── scripts/bootstrap.py  # 演示数据初始化
└── tests/                # 纯逻辑单测（不依赖 DB）
```

## 核心设计

### 两套密码存储逻辑（TODO 决策 #5）

| 用途 | 模块 | 算法 | 密钥 |
| ---- | ---- | ---- | ---- |
| 本系统登录密码 | `utils/crypt.py` | bcrypt 单向哈希 | 无 |
| 中台账号密码（要代登） | `utils/secret_box.py` | Fernet 可逆加密 | `CREDENTIAL_ENCRYPTION_KEY` |
| 上游模型服务 API Key | `utils/secret_box.py` | Fernet 可逆加密 | `PROVIDER_ENCRYPTION_KEY` |

两把可逆密钥必须分开保管：一把泄露不应牵连另一类数据。密钥轮换把旧密钥挂到
`*_ENCRYPTION_KEYS_OLD`（逗号分隔），旧密文照常解密，再分批 `rotate_secret` 重加密。
中台凭证的每次解密都要写 `credential_access_logs`。

### 角色与权限

角色定义在 `model/database.py`，权限判定统一在 `services/permissions.py`。
字段名沿用 `permission` 以兼容既有 JWT 载荷。

| 角色 | 级别 | 说明 |
| ---- | ---- | ---- |
| `president` 总裁 | 100 | 最高权限（admin），不能被任何人管理 |
| `developer` 开发者 | 90 | 可视组件与权限和总裁一致；唯一区别是不能管理总裁账号 |
| `auditor` 高管/审计 | 60 | 无配置权限；可查看会话内容（按角色，不看部门） |
| `manager` 部门负责人 | 40 | 暂无管理端权限 |
| `employee` 普通员工 | 10 | 只用客户端 |

遗留值 `admin` 等同 developer（级别 90），`operator` 等同 employee。

**三套正交的规则：**

1. **功能权限**（`PERMISSIONS` 表，按角色）：`dept:*` / `user:*` / `model:*`
   开放给总裁与开发者。依赖项 `require_permission("dept:edit")` 做校验，
   **没有超级角色兜底** —— 总裁能过是因为表里写了它，而不是某个角色被硬编码成万能。

2. **会话内容可见性**（`audit:view`，**按角色**）：总裁、开发者、管理部门审计三个角色可见，
   部门负责人与普通员工不可见，不再检查账号所属部门。
   （旧版要求「角色 + 部门是管理部门」双重满足，部门没填对的审计员什么都看不到。）

3. **账号管理层级**（`can_manage`）：只能操作级别**严格低于**自己的账号，
   唯一例外是开发者可以管理其他开发者。于是总裁可管理开发者；开发者可管理
   除总裁以外的所有账号，动不了总裁；开发者可以再设开发者，但设不了总裁。

4. **用量报表的数据范围**（`resolve_usage_scope`）：能打开报表页是一回事
   （功能权限 `usage:view`），能看到**谁的**数字是另一回事。

   | 角色 | 能进报表页 | 看得到谁 |
   | ---- | ---------- | -------- |
   | 总裁 / 开发者 | ✅ | 全公司（`usage:view:all`） |
   | 审计 / 部门负责人 | ✅ | 仅本部门 |
   | 员工 | ❌ | —— |

   注意这条**不像 audit:view 那样看全公司**：管理部门审计能看全公司聊天内容，
   但用量报表仍只看本部门。两套规则各有各的依据，别互相套用。

   没有部门的负责人会**降级成只看自己** —— 否则 `dept_id IS NULL` 这个条件
   会把所有无部门账号的用量一起漏出去。

审计部门锚点是 `model/org_defaults.py` 的 `AUDIT_DEPT_CODE`（默认 `exec`）。

规则全部覆盖在 `tests/test_permissions.py`。前端 `constants/roles.ts` 是这份表的镜像，
只影响界面显示，改动必须两边同步。

### 组织架构

管理部门、示例部门与管理员账号「admin」在 `init_db()` 建表时
幂等写入，定义见 `model/org_defaults.py`。

- 按 `code` 判重，后台改过的部门名不会被重启打回
- 只在「一个总裁都没有」时创建超管，初始密码随机生成并在日志里**打印一次**
- 用户名直接用中文花名（如「admin」），不强制英文

会话内容查看权按角色授予（总裁 / 开发者 / 管理部门审计）。管理部门（`exec`）仍是
提示词策略页「管理部门负责人」等按部门判定的规则的锚点。

### 模型白名单（决策 #6、#7）

四类作用域：`global` / `dept` / `role` / `user`。

- 允许集合 = 四类 `allowed=True` 规则的并集
- 任一 `allowed=False` 命中即拒绝，优先级高于所有允许规则
- **默认拒绝**：没有任何规则时该用户没有可用模型（含总裁）
- 管理端权限与模型授权是两回事：能配白名单不等于自己能用，避免审计口径含糊

当前按约定**不预置任何白名单**，模型逐部门在后台手工开通。
在配置之前所有人都没有可用模型，这是预期行为不是 bug。

判定逻辑在 `services/model_access.py`，纯函数、无数据库依赖，测试见 `tests/test_model_access.py`。

### 转发链路（`router/v1/chat.py`）

```
认证（JWT / API Key）→ 按别名或模型名找模型 → 白名单判定（默认拒绝）
  → 发送方向脱敏兜底 → 转发上游（流式/非流式）
  → 写用量明细 + 日聚合 + 会话审计
```

**这是审计、脱敏、用量三件事唯一的写入点**，新逻辑请集中放在这条路径上，
散到别处很容易出现「某个入口没记账」的黑洞。

几个容易踩的点：

- **流式必须带 `stream_options: {include_usage: true}`**，否则 SSE 最后一个
  chunk 里没有 usage，token 数只能估算。网关强制加上，不指望客户端记得传。
- 上游没返回 usage 时按字符估算，并写 `tokens_estimated=true`，
  报表必须把估算值与实测值分开展示，否则口径是糊的。
- SSE **原样透传**不重新序列化，避免破坏上游的字段与顺序；解析只用于旁路统计。
- 流式响应头已发出后再出错，没法改 HTTP 状态码，只能补一个 error 事件 + `[DONE]`。
- 失败也记一笔用量（`status=failed`），上游抖动频率是评估合作厂商稳定性的依据。
- 统计写失败**不会**让用户请求跟着失败，但一定留 error 日志，否则报表少数据没人知道。

### 客户端认证：两条通道

| 通道 | 用途 | 形式 |
| ---- | ---- | ---- |
| JWT | 管理后台（浏览器） | httpOnly Cookie / `Authorization: Bearer <access_token>` |
| API Key | 桌面客户端（hermes） | `Authorization: Bearer sk-llano-xxx` |

Key 存 sha256 不存明文（40 位密码学随机，熵足够，不必用 bcrypt 那种故意的慢），
按前缀列索引定位再比对哈希。可吊销、可设有效期，明文只在签发时回显一次。

### 数据表分工

| 表 | 装什么 | 谁能看 |
| -- | ------ | ------ |
| `conversation_logs` | 聊天原文（脱敏后） | 仅管理部门 |
| `usage_records` | 每次调用的指标明细 | 报表 / 部门负责人 |
| `usage_daily` | 日期 × 用户 × 模型 的预聚合 | 报表页只查这张 |

内容与指标分表是刻意的：看报表的人不该顺带碰到聊天内容。
明细里的部门、角色存**快照**，调岗改名之后历史报表不会跟着变。

### token 明细：先全存，后算钱

真实计费口径远比「输入×单价 + 输出×单价」复杂：缓存读取、缓存写入、按次请求、
分段阶梯、Anthropic 的分时缓存（5m/1h 两档 TTL 不同倍率）……各家字段名还都不一样。

**我们不做计费引擎，但把原始数据全部存下来。** 理由是这两件事性质不同：

- **token 明细是一次性的**：这一刻不落库，以后再想算「缓存到底省了多少」永远算不出来。
- **定价规则是可回溯的**：只要原始 usage 在，规则怎么改都能重算历史。

所以 `usage_records` 里除了三个基本 token 数，还有：

| 列 | 内容 |
| -- | ---- |
| `cached_prompt_tokens` | 命中缓存的输入 token（读缓存，通常更便宜） |
| `cache_write_tokens` | 写入缓存的 token（建缓存，通常更贵） |
| `reasoning_tokens` | 推理模型的思考 token |
| `usage_raw` | **上游 usage 对象原文 JSON** |

前三列是 `extract_breakdown()` 对各家方言做的归一化（OpenAI 的
`prompt_tokens_details.cached_tokens`、DeepSeek 的 `prompt_cache_hit_tokens`、
Anthropic 的 `cache_read_input_tokens` 都会落到同一列）。
**归一化只是为了报表好查，不是为了算钱** —— 认不出来的字段全在 `usage_raw` 里。

报表里的「估算费用」只是模型上两个线性单价（元/千 token）算出来的粗略分摊：

- 单价 **NULL 表示未配置**，与 0（免费）语义不同。费用算不全时报表显示「—」，
  **绝不按 0 计入总额** —— 偏小的总额看起来完全正常，是最难发现的那种错。
- 这是**按当前单价回算历史用量**，上游调价后历史费用会跟着变。
  要金额也不漂移得在明细里存单价快照，那是成本核算功能的事，现在不做。
- 钱的唯一真源是合作厂商的月度账单。报表口径已写死在 `USAGE_NOTE` 里，
  连导出的 CSV 也会带上这句。

### 用量报表（读取侧）

写入侧挂在 `/v1/chat/completions` 上，读取侧是 `router/admin/usage.py`。分工别越界：

- **概览 / 趋势 / 下钻**一律查 `usage_daily`（已按 日期 × 用户 × 模型 预聚合）
- **只有明细页**查 `usage_records`，且强制分页 + 强制带时间范围

二次聚合在 Python 里做而不是拼 SQL：维度有 5 种、粒度有 3 种，
写进 SQL 会变成一坨 if，而日聚合表量级很小（人数 × 模型数 × 天数）拉回来算完全够用，
更重要的是这段逻辑能脱离数据库单测（`tests/test_usage_query.py`）。
真扛不住那天，再针对最热的两三种组合下推 SQL 不迟。

几个已经踩过的点：

- 周分桶按**周一**为一周之始，跨月的那一周不能被月份切成两半。
- 桶 key 必须补零成定长串（`2026-01` 而非 `2026-1`），因为它直接当排序键用。
- 平均耗时用整数运算而不是 `round()`：后者是银行家舍入，150.5 会变成 150，
  同一份数据看两眼像在跳动。
- 趋势图的空桶要**显式补 0**，不能靠「没数据就不画点」——
  否则停机三天会被看成正常波动。
- 查询跨度上限 366 天。允许「不带条件查一下」就等于允许把库查挂。

## 接口

| 方法 | 路径 | 权限 | 说明 |
| ---- | ---- | ---- | ---- |
| GET | `/healthz` | 公开 | 存活探针 |
| GET | `/readyz` | 公开 | 就绪探针（探活 MySQL/Redis，不通返回 503） |
| POST | `/admin/user/login` | 公开 | 登录 |
| GET | `/admin/user/info` | 登录 | 当前用户信息 |
| POST | `/admin/user/refresh` | 公开 | 刷新 Token（refresh_token 取请求体，缺省回退读 Cookie） |
| POST | `/admin/user/logout` | 登录 | 登出：撤销 access/refresh（桌面端可在请求体提交 refresh token）、清 Cookie/缓存 |
| GET | `/admin/department/list` | dept:view | 部门列表 |
| POST | `/admin/department/create` | dept:edit | 新建部门 |
| POST | `/admin/department/update/{id}` | dept:edit | 修改/停用部门 |
| GET | `/admin/department/users/{id}` | dept:view | 部门成员 |
| POST | `/admin/department/transfer` | dept:edit | 批量转移成员 |
| GET | `/admin/account/list` | user:view | 账号列表（含 manageable 标记） |
| GET | `/admin/account/me/context` | user:view | 当前操作者可分配的角色与权限点 |
| POST | `/admin/account/create` | user:edit | 新建账号，随机密码只回显一次 |
| POST | `/admin/account/assign` | user:edit | 调整部门/角色（受级别限制） |
| POST | `/admin/account/status` | user:edit | 启用/禁用（总裁、开发者停用低级账号走这里） |
| POST | `/admin/account/reset-password` | user:edit | 重置密码 |
| GET/POST | `/admin/catalog/provider/*` | model:view/edit | 上游供应商增改查 |
| GET/POST | `/admin/catalog/model/*` | model:view/edit | 模型增改查/停用 |
| GET/POST | `/admin/catalog/whitelist/*` | model:view/edit | 白名单增改删查 |
| GET | `/admin/usage/summary` | usage:view | 区间合计 + 按 日/周/月 的趋势（空桶补 0） |
| GET | `/admin/usage/breakdown` | usage:view | 按 人/部门/角色/模型/时间 下钻，筛选条件可叠加 |
| GET | `/admin/usage/records` | usage:view | 单次调用明细（唯一查明细表的接口，强制分页） |
| GET | `/admin/usage/filters` | usage:view | 筛选下拉框候选项（只列范围内确实有用量的） |
| GET | `/admin/usage/export` | usage:view | 导出当前下钻结果为 CSV（**返回文件流，不套信封**） |
| GET | `/admin/apikey/list` | 登录 | 我的 API Key（管理下级需级别更高） |
| POST | `/admin/apikey/create` | 登录 | 签发 Key，明文只回显一次 |
| POST | `/admin/apikey/revoke/{id}` | 登录 | 吊销 Key |
| GET | `/client/models` | 登录 | 当前用户可用模型（后台展示用） |
| GET | `/client/models/preview/{uid}` | model:view | 预览指定用户的最终可用模型 |
| **POST** | **`/v1/chat/completions`** | JWT 或 API Key | **对话转发**（流式/非流式），审计与用量的写入点 |
| **GET** | **`/v1/models`** | JWT 或 API Key | OpenAI 格式的可用模型列表 |
| POST | `/v1/device/revoke` | 设备 API Key | 只允许当前设备 Key 自助吊销自身（用于离线退出补偿） |
| GET/POST | `/admin/mcp/*` | mcp:view/edit | MCP 实例、动态配置、能力目录、用户授权与调用日志 |
| GET | `/v1/mcp/instances/{instance_code}/credential` | JWT 或 API Key | 查看当前用户自己的上游账号配置/验证状态，不回显账号或密码 |
| PUT | `/v1/mcp/instances/{instance_code}/credential` | JWT 或 API Key | 绑定或替换当前用户自己的上游账号；先验证成功再加密保存 |
| POST | `/v1/mcp/instances/{instance_code}/credential/test` | JWT 或 API Key | 重新验证当前用户已保存的上游账号 |
| DELETE | `/v1/mcp/instances/{instance_code}/credential` | JWT 或 API Key | 清除当前用户自己的上游账号 |
| POST | `/v1/mcp/capabilities/search` | JWT 或 API Key | 两层能力目录：首层返回分类统计，传 `domain` 下钻接口；标注可用/需绑定账号/待授权/过期/停用，仅可用项返回输入 Schema |
| POST | `/v1/mcp/execute` | JWT 或 API Key | 按能力短码执行；服务端二次鉴权并记录审计 |

`/v1/*` 是 OpenAI 兼容接口，响应**不套** `{code,message,data}` 信封，
否则客户端的 OpenAI SDK 解析不了；API Key 失效统一返回 HTTP 401 + OpenAI error。
`/admin/usage/export` 同理 ——
它返回的是文件流，套了信封就变成一个内容是 JSON 的 .csv。

`/admin/usage/*` 的数据范围由登录态算出（见上文「用量报表的数据范围」），
**不看前端传了什么**：前端传的 `dept_id` 只是「想看哪个部门」，能不能看是另一回事。

所有响应统一为 `{code, message, data}`，错误码见 `model/response.py`。
响应头 `X-Request-Id` 用于串联日志与后续审计记录。

## 快速开始

直连本地已有的 MySQL 与 Redis，不需要容器。

```bash
cd audit-gateway/server

# 1. 建库（只需一次）
mysql -u root -p -e "CREATE DATABASE audit_gateway DEFAULT CHARACTER SET utf8mb4;"

# 2. 装依赖
pip install -r requirements.txt

# 3. 配置 .env.dev
cp .env.example .env.dev
python -c "import secrets; print(secrets.token_urlsafe(48))"                               # JWT 两把
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # 加密两把

# 4. 启动
#    首次会建表 + 写入 15 个部门 + 创建总裁账号「admin」，
#    初始密码在日志里打印一次，记下来立刻登录改掉
python main.py

# 5. 登记上游渠道并同步模型清单（地址与 Key 读 .env.dev）
python scripts/bootstrap.py
```

⚠️ **库不是新建的话，还要补跑迁移**。`create_all` 只建缺失的表，不会给已有表加列：

```bash
# ⚠️ 必须按文件名顺序跑全，跳着跑会留下一个「半新半旧」的库
mysql -u root -p audit_gateway < scripts/migrations/2026-08-07-usage-reporting.sql
mysql -u root -p audit_gateway < scripts/migrations/2026-08-08-conversation-audit.sql
mysql -u root -p audit_gateway < scripts/migrations/2026-08-08-timezone-and-pricing.sql
mysql -u root -p audit_gateway < scripts/migrations/2026-08-08-billing-modes-and-abort.sql
mysql -u root -p audit_gateway < scripts/migrations/2026-08-08-price-snapshot.sql
mysql -u root -p audit_gateway < scripts/migrations/2026-08-13-api-key-quota.sql
```

漏了这步不会启动失败，而是等到第一次转发写用量时才炸 `Unknown column`。
全新的库跳过即可（`create_all` 会照 ORM 建全）。

启动自检 `db.check_schema_drift` 会比对 ORM 与实际表结构：
dev 只告警、prod 拒绝启动 —— 但它只查缺列，跑到一半的迁移仍然要人自己盯。

⚠️ **两把 Fernet 密钥必须是 32 字节 urlsafe base64**（44 位、以 `=` 结尾），
随手写的随机串会在加密时直接报错。`*_KEYS_OLD` 平时留空，只在轮换主密钥时才填。

监听端口由 `.env` 的 `PORT` 决定（默认 7899），改了要同步前端
`client/vite.config.mts` 的 `GATEWAY_ORIGIN`。

脚本会调上游的 `/v1/models` 自动发现模型；上游不支持时用
`--models a,b,c` 手工指定。**不会预置任何白名单** —— 模型逐部门在后台开通。

macOS 上如果报 `CERTIFICATE_VERIFY_FAILED`，是 Python 缺根证书，
运行一次 `/Applications/Python 3.x/Install Certificates.command` 即可；
应急可加 `--insecure` 跳过校验（仅对可信上游）。

生产：`ENVIRONMENT=prod gunicorn -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 main:app`

启动自检会检查 JWT 与两把加密密钥；**dev 只告警，prod 缺配置直接拒绝启动**。

## 测试

```bash
pytest
```

当前测试为不依赖 MySQL/Redis 的纯逻辑单测：

- `tests/test_permissions.py` —— 角色权限与账号管理层级（谁能停用谁、谁能看审计）
- `tests/test_model_access.py` —— 白名单判定
- `tests/test_secret_box.py` —— 两套密码逻辑与密钥轮换
- `tests/test_user_lookup.py` —— 用户查询条件（登录曾把用户名当主键查的回归）
- `tests/test_forwarding.py` —— 请求体改写、SSE 解析、用量提取、脱敏
- `tests/test_apikey.py` —— Key 生成、校验、掩码
- `tests/test_usage_query.py` —— 报表分桶、聚合、费用估算、CSV 导出
- `tests/test_mcp_platform.py` —— MCP 权限模式、受信任目录、入参 Schema、范围约束与签名算法
- `tests/test_internal_datahub.py` —— 中台直连登录、Token 生命周期、固定只读请求和超时/错误分类
- `tests/test_mcp_credentials.py` —— 每用户凭据所有权、加密、状态接口与解密审计

### MCP 权限模式与中台凭据

MCP adapter 必须显式声明权限模式：

- `local_grants`：领星沿用 `mcp_user_permissions` 默认拒绝，并在服务端执行店铺、仓库、分页、时间范围等约束。
- `upstream_account`：`internal_datahub` 要求调用者绑定并测试自己的中台账号，不读取或创建本地 capability 授权；中台 OA 审批后的账号权限是业务数据权限唯一真源。

管理员不能查看用户的中台密码、借用用户凭据或在本系统扩大中台权限。账号密码只在绑定请求和服务端解密后短暂存在，密码使用 `CREDENTIAL_ENCRYPTION_KEY` 对应的 Fernet 密钥保存；每次创建、更新、测试、解密和清除都写 `credential_access_logs`。状态接口永不返回账号、密码或 Token。

`internal_datahub` 首批只允许两个已实测的固定只读能力：

- `datahub.report.platform.list` → `POST /api/Platform/GetList`
- `datahub.dcms.brand.list` → `POST /api/Brand/GetBrandList`

运行时只使用服务端 `httpx` 直连接口，不依赖 Playwright、Selenium、浏览器 Cookie 或浏览器 sidecar。若上游要求图片验证码、短信/OTP、二次验证或强制改密，adapter 立即停止并返回明确错误，不实现或绕过验证。超时返回 HTTP 504；连接失败、上游拒绝和非法响应返回脱敏的 HTTP 502 分类，并写 MCP 调用日志。

当前中台认证与业务接口仍是 HTTP。生产启用前应推动外包方提供 HTTPS；未完成前只允许受控网络出口，并由负责人明确接受链路可能被窃听或篡改的风险。

### MCP 领星配置注意

后台「MCP 管理」按适配器 Schema 动态渲染。领星的 `App ID`、`App Secret`
与正向代理 URL 都属于敏感字段，只写入服务端 Fernet 密文，读取 API 只返回字段是否已配置。
若领星设置了固定出口 IP 白名单，gost 常见配置提供的是 HTTP CONNECT 代理，地址应写成
`http://user:password@host:port`；不要因目标站点是 HTTPS 就把代理协议写成 `https://`。

中台纵向闭环使用库名强制以 `_test` 结尾的独立 MySQL，测试会清理涉及的七张表，禁止指向开发/生产库：

```bash
AUDIT_GATEWAY_TEST_MYSQL_URL='mysql+aiomysql://test_user:test_password@127.0.0.1:3306/audit_gateway_test?charset=utf8mb4' \
CREDENTIAL_ENCRYPTION_KEY='MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=' \
python -m pytest -q tests/integration/test_internal_datahub_flow.py
```

示例 Fernet Key 只用于隔离测试库，不得部署到任何环境。自动化测试不读取真实中台账号、`bp_keys` 或生产 Token；外部 HTTP 全部由 `httpx.MockTransport` 接管。

## 管理后台前端

`../client` 是配套的 Vuetify 管理后台，开发时用 vite proxy 与本服务同源，
否则 httpOnly Cookie 跨站不会被带上。生产环境若前后端不同源，
需在 `.env.prod` 配 `CORS_ALLOW_ORIGINS`。

## 两条全局口径（改之前先读）

**时间一律是服务器本地时间。** 唯一来源是 `model/database.py` 的 `now()`，
部署要求服务器时区为 `Asia/Shanghai`。曾经时间戳存 UTC、`stat_date` 存本地日，
结果审计页时间早 8 小时，且 00:00~08:00 的调用在两个页面被算成不同的日期。
新增时间列一律用 `now()` 作默认值，别再引入第二种时钟。

**单价一律是「元 / 百万 token」**（决策 #25），常量在
`services/usage_query.py` 的 `TOKENS_PER_PRICE_UNIT`。跟厂商价目表口径一致，
后台可直接照抄。改成千 token 不会让任何测试崩，只会让金额悄悄差 1000 倍
—— `tests/test_usage_query.py::test_price_unit_is_per_million` 就是为此存在的。

## 计价模式（决策 #27，部分修订 #21）

两种模式，`models.billing_mode` 区分：

| 模式 | 算法 |
| ---- | ---- |
| `token` | `(输入−缓存命中)×输入价 + 缓存命中×缓存价 + 输出×输出价` |
| `per_call` | `调用次数 × 每次单价`，与 token 数无关 |

**阶梯 / 分段 / 分时缓存仍然不做。** 真要做时，原始 usage 全在
`usage_records.usage_raw` 里，可以随时重算历史，不必现在预留。

两个容易出错的地方，都有断言守着：

1. **上游返回的 `prompt_tokens` 是含缓存命中的总数**，不减出来就等于
   缓存那段按全价又算了一遍。
2. **缓存单价留空 = 「没单独配」→ 回退到输入单价，不是免费。**
   按 0 算等于宣称缓存不要钱，费用会凭空偏低，而偏低的金额看着完全正常。

⚠️ 缓存单价只有在上游确实返回缓存 token 时才有意义。填之前先跑：

```bash
python scripts/probe_upstream_usage.py --db-only          # 翻旧账，不花钱
python scripts/probe_upstream_usage.py --user admin --password '...'   # 再打两次真实请求
```

它会同时扫 `usage_raw` 历史记录、连发两次相同长前缀的请求，最后给出
「上游到底返不返回缓存字段」的结论。不返回就别填 —— 配了也没有 token 数可乘。

## 中断 ≠ 失败（决策 #28）

用户点「停止生成」时，`usage_records.aborted` 置 1，但 **`status` 仍然是
`success`**、`error_code` 为空。

这次调用真的发生了、token 真的花了、内容也真的产出了，只是没人看完。
写进 `status` 会污染「失败率」—— 那个指标是用来判断合作厂商上游稳不稳的，
掺进用户主动叫停就失去意义了。前端用一个独立的「中断」chip 展示。

## 记账不能挂在请求生命周期上

流式转发的 `record_usage` **必须放在生成器的 `finally` 里，并以后台任务执行**
（`router/v1/chat.py` 的 `_record_in_background`）。

这是实测抓出来的漏洞：发了 4 次请求只记了 3 次，漏的是流式那次。原先记账写在
最后一个 `yield` 之后，客户端收到 `[DONE]` 就断开，Starlette 随即取消生成器，
执行流走不到记账。真实场景就是用户点「停止生成」，而且越长的回答越容易被叫停,
漏掉的恰好是最贵的请求。它不报错、不留日志，报表上少一笔和正常毫无区别。

不能直接 `await`：`finally` 可能由 `GeneratorExit` 唤醒，在关闭中的异步生成器里
await 会抛 `RuntimeError`。任务要存进模块级 set 保住强引用，否则可能被 GC。

回归测试：`tests/test_stream_billing.py`。**新增任何出网调用都照这个模式写。**

## 会话审计（决策 #8）

写入挂在转发链路上，读取是 `/admin/audit/*`，两侧的规矩都写在
`services/attachments.py` 与 `router/admin/audit.py` 的模块 docstring 里。
这里只记三条最容易被后人无意破坏的：

1. **列表接口绝不下发全文。** 全文只能走 `/detail/{id}`，于是每次
   「看了谁的哪一条」都能精确落进 `audit_access_logs`。哪天有人想
   「顺手在列表里带上全文省一次请求」，查阅日志的颗粒度就一起没了。
2. **附件必须在序列化之前抽走。** 内联 base64 图片有几 MB，
   直接 `json.dumps` 之后截断会从中间一刀切下去，正文里最想看的文字反而没了。
3. **远程 URL 形态的附件只记不抓。** 网关主动下载用户给的任意地址是个
   SSRF 面，不值得为审计完整性开这个口子。

可见性判定只有 `can_view_audit` 一条路（**按部门**：总裁无条件，
其他人须在管理部门且级别 ≥ 审计员）。与用量报表的 `resolve_usage_scope`
完全独立 —— 管理部门审计员看得到全公司聊天内容，但用量仍只看本部门。

端到端验证脚本：

```bash
python scripts/e2e_audit_check.py --user admin --password '...' --model deepseek-v4-flash
```

会真发文本/图片/文件/流式四种请求打真实上游，再回头核对
会话存读、附件落盘回读（比对 sha256）、查阅日志、用量计费四条链路。
**会产生真实费用和真实记录，别在生产库上反复跑。**

## 已知待办

- [x] **单价快照**：记账时把当时生效的单价与算出的费用一并落库
      （`usage_records` 存单价与 `cost`，`usage_daily` 存 `cost_sum` +
      `snapshot_requests` / `priced_requests` 两个计数器）。
      报表优先用快照，只有快照缺失（上线前的历史数据）才退回按当前单价回算，
      并用 `cost_basis` 标出来是哪一种。缓存读取与按次计价已落地（决策 #27）；
      阶梯/分段计价按决策继续不做
- [ ] 与合作厂商月度账单对账：报表费用仍是估算（口径来自上游 usage），
      拿到账单后可直接用 `usage_raw` 按账单口径重算历史
- [ ] 会话内容加密存储 + 留存期限自动清理（第 5 节合规动作，
      附件已按 `<年月>/<哈希前缀>/` 分目录，整月删除很方便）
- [ ] 会话内容检索目前是 `LIKE '%kw%'` 全表扫描（靠强制时间范围兜着），
      量大了要上全文索引或 ES
- [ ] 限流与配额：按人/部门的日 token 上限，超了拒绝或降级
- [x] 中台每用户凭证绑定/测试/清除接口 + 解密审计日志落库
- [ ] 改用 Alembic 管理迁移（当前是 `create_all` 自动建表，仅够原型阶段）
      ⚠️ **`create_all` 只建缺失的表，不给已有表加列**。所以每次加字段都要
      手工补一份 SQL 到 `scripts/migrations/`，已经跑过的库必须执行它，
      否则启动不报错、写入时才炸在 Unknown column 上
- [ ] 改角色/部门后主动清理 Redis 里的用户信息缓存（当前需重新登录或等缓存过期）
- [ ] 用量明细表按月分区或定期归档（半年后会很大，报表只查日聚合表能缓解）
