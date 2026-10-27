# Codex 上游渠道（从 codex2api 提取的核心）

把一个 ChatGPT / Codex 账号变成审计网关里的一条上游渠道：员工照常调
`/v1/chat/completions`，网关把请求翻译成 Codex Responses 协议发给
`chatgpt.com/backend-api/codex`，响应再翻回 OpenAI 兼容格式，审计、配额、
脱敏、记账全部照旧生效。

只保留了 codex2api 的三段核心，其余（账号池调度、管理台、计费、生图、
Grok/Claude 多家适配、代理池……）都没有带进来：

| 核心 | 代码 | 来源 |
| --- | --- | --- |
| 登录（OAuth PKCE） | `services/codex_channel/oauth.py` | codex2api `admin/oauth.go` |
| 获取凭证（续期/轮换） | `services/codex_channel/credentials.py` | codex2api `auth/token.go` |
| 连通（协议翻译 + 出站） | `services/codex_channel/translate.py`、`upstream.py` | codex2api `proxy/translator.go`、`proxy/executor.go` |

## 工作方式

- Codex 渠道在 `upstream_providers` 表里以 **`codex://` 哨兵地址**标识
  （默认 `codex://chatgpt`）。转发层 `services/forwarding.py` 识别后走
  Codex 适配层，其余渠道仍走标准 OpenAI 兼容转发，链路完全不变。
- 凭证（refresh_token / access_token）与普通上游 API Key 同一条 Fernet
  加密通道存进 `api_key_encrypted`，只进不出，任何接口都不回带 token 明文。
- access_token 过期前 60 秒自动续期；OpenAI 轮换的 refresh_token 会立即
  写回库。同一渠道通过数据库行锁串行续期，锁内重读最新凭证，支持多个服务进程。
- 出站按 Codex CLI 0.153.3 的形态（UA / Originator / Version /
  Beta-Features / Chatgpt-Account-Id），与 codex2api 同口径。
- 请求里的审计会话号只以哈希形式进上游 `prompt_cache_key`：同会话多轮
  落同一缓存键（省钱提速），不同会话互相隔离，原始会话号不出网关。

## 管理员操作

### 1. 登录拿凭证（二选一）

**方式 A：浏览器授权（推荐）**

第一步，发起登录（生成授权 URL）：

```bash
curl -X POST http://<网关>/admin/codex/oauth/start \
  -H "Authorization: Bearer <管理端token>" \
  -H "Content-Type: application/json" -d '{}'
# → {"data": {"session_id": "...", "auth_url": "https://auth.openai.com/oauth/authorize?..."}}
```

**在浏览器里打开 auth_url，用 ChatGPT/Codex 账号完成授权**（这一步必须是
人来做，AI 不碰账号密码/扫码/验证码）。授权后浏览器会跳到
`http://localhost:1455/auth/callback?code=...&state=...`（页面打不开是正常
的），把**地址栏整段 URL** 复制下来。

第二步，由同一管理员粘贴含 code 和 state 的完整回调 URL 兑换凭证并落库。
网关 OAuth 会话存储在 Redis，30 分钟有效、一次使用，并绑定发起管理员；普通上游编码不能被覆盖为 Codex 渠道。


```bash
curl -X POST http://<网关>/admin/codex/oauth/exchange \
  -H "Authorization: Bearer <管理端token>" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "<第一步的session_id>",
       "code": "<粘整条回调URL>",
       "provider_code": "codex-main",
       "name": "Codex主号"}'
# → {"data": {"provider_id": 1, "email": "...", "plan_type": "..."}}
```

**方式 B：直接导入 refresh_token**

已有 `codex login` 拿到的 refresh_token 时可以直接导入（导入时会先换一次
access_token 验证有效性并拿到账号身份）：

```bash
curl -X POST http://<网关>/admin/codex/provider/import \
  -H "Authorization: Bearer <管理端token>" \
  -H "Content-Type: application/json" \
  -d '{"provider_code": "codex-main", "refresh_token": "<RT>"}'
```

### 2. 连通探测 + 确认可用模型

```bash
curl -X POST http://<网关>/admin/codex/provider/1/probe \
  -H "Authorization: Bearer <管理端token>"
# → {"data": {"ok": true, "models": ["gpt-5.4-codex", ...], "raw": {...}}}
```

### 3. 绑定模型（复用现有模型管理接口）

```bash
curl -X POST http://<网关>/admin/catalog/model/create \
  -H "Authorization: Bearer <管理端token>" \
  -H "Content-Type: application/json" \
  -d '{"provider_id": 1, "model_name": "gpt-5.4-codex",
       "display_name": "Codex 5.4", "alias": "codex-5.4",
       "max_context_tokens": 400000}'
```

之后按现有流程配白名单/权限/日配额即可，员工端切到 `codex-5.4` 就能用。

### 4. 查看渠道状态

```bash
curl http://<网关>/admin/codex/provider/1/status \
  -H "Authorization: Bearer <管理端token>"
# → 账号邮箱 / 套餐 / access_token 有效期（不回带 token 明文）
```

## 剩余额度与掉号检测

### 剩余额度

```bash
curl http://<网关>/admin/codex/provider/1/usage -H "Authorization: Bearer <管理端token>"
# → {"data": {"usage": {"email": "...", "plan_type": "pro",
#      "primary_window": {"used_percent": 4.0, "window_seconds": 604800, ...},
#      "reset_credits": {"available_count": 3, ...}}}}
```

数据源是 ChatGPT 后端的 `wham/usage`，返回账号此刻真实的用量窗口。**窗口含义看
`window_seconds`**，别看 primary/secondary 的位置：18000 = 5h 窗口，604800 = 7d
窗口，账号/套餐不同主窗口可能是任意一个。命令行同款查询：

```bash
python scripts/codex_login.py usage --provider-id 1
# → 168h 窗口: 已用 4.0%  约 4天21小时 后重置
#   限速状态: 正常  重置券: 3 张
```

### 掉号检测

「掉号」= refresh_token 被撤销 / 账号被封 / 授权过期。三条腿检测：

1. **流量侧被动标记**：任何请求遇到凭证类失败（invalid_grant、上游 401），
   渠道立刻被记成 `dead`，员工报错与后台状态同源；
2. **周期巡检**：每 10 分钟对每条 Codex 渠道做一次「续期 + 额度查询」，空闲渠道
   掉了也能发现。间隔由 `CODEX_HEALTH_INTERVAL_SECONDS` 控制（0 关闭）；
3. **随时可查**：`GET /admin/codex/provider/1/status` 返回的 `health` 字段
   （`alive` / `dead` / `limited` / `unknown` + 原因 + 检查时间）。健康摘要目前为进程内缓存，多 worker 间检查时间可能不同；凭据和 OAuth 会话为共享存储。

掉号后请求会以 `503 codex_login_required` 失败，重新登录即恢复，无需重启服务：

```bash
python scripts/codex_login.py start        # 重新走两步登录
```

## 边界与注意

- **单账号渠道**：一条渠道 = 一个账号。要多账号负载就建多条渠道分别绑模型；
  codex2api 的账号池调度/健康评分没有提取（那是臃肿的主要来源）。
- **凭证彻底失效**（refresh_token 被撤销）：请求会以 503 `codex_login_required`
  失败，管理端重新走一遍登录即可。
- **上游 401**：对员工响应为 503 `codex_auth_failed`，避免误触发员工登录失效；临时续期失败为 502 `codex_refresh_unavailable`，不会标记凭据永久失效。
- **流式异常**：SSE failed 事件或无终结事件的提前断流按失败处理，不计为成功响应。
- **采样参数被丢弃**：temperature / top_p / stop 等 Codex 后端不接受，网关
  翻译时会剥掉（这是 codex2api 的既有口径，发了会 400）。
- **上游限速**：ChatGPT/Codex 账号有 5h/7d 用量窗口，超过会限速；这是账号
  本身的产品约束，网关不做绕过。
- **合规提示**：把 ChatGPT 订阅账号当 API 中转使用存在违反 OpenAI 服务条款
  的风险，是否在贵司合规范围内请自行判断；账号来源与用途由部署方负责。
