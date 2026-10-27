# audit-gateway / client

网关管理后台前端（Vue 3 + Vuetify 4 + Pinia + Vue Router）。
当前完成度：**登录、布局壳、角色权限体系、部门管理、用户与角色、模型与白名单**。
会话审计页待后端实现后接入。

后端接口见 `../server/README.md`，角色与权限的最终判定在网关，不在这里。

## 快速开始

```bash
# 1. 先起网关（另开一个终端，需要本地 MySQL + Redis 已在跑）
cd ../server && python main.py

# 2. 起前端
npm install
npm run dev          # http://localhost:3000
```

开发环境通过 Vite proxy 把 `/admin`、`/client`、`/healthz`、`/readyz` 转发到
`http://127.0.0.1:7899`。如需连接公司内网测试网关，在 `.env.development.local`
中设置仅供 Vite 服务端读取的地址：

```dotenv
GATEWAY_ORIGIN=http://<公司内网固定IP>:7899
```

浏览器端请求始终使用 `/admin/...` 这类相对路径。**不要设置 `VITE_API_BASE`，也不要
让浏览器直连 7899/8000**：后端下发的是 httpOnly Cookie，跨站后不会被带上，刷新页面
就会失去登录态。测试和生产环境也应由同一个域名反向代理前端与网关；公网地址稳定后只
调整反向代理，不重新构建前端。

首次启动网关时终端会打印初始管理员账号密码（账号 `admin`）。

## 目录结构

```
src/
├── api/
│   ├── http.ts          # fetch 封装：统一 code 处理、自动刷新令牌、错误收敛成 ApiError
│   ├── auth.ts          # 登录 / 当前用户 / 登出
│   ├── department.ts    # 部门增改查、成员、批量转移
│   ├── account.ts       # 账号增改、角色部门调整、启停、重置密码
│   ├── catalog.ts       # 渠道 / 模型 / 白名单 / 用户可用模型预览
│   └── system.ts        # /healthz /readyz（裸 JSON，不走统一信封）
├── constants/roles.ts   # 角色展示 + 级别；功能权限以登录上下文的 permissions 为准
├── composables/
│   ├── usePermission.ts # can() / hasRole() / isAdmin
│   └── useMenus.ts      # 按角色过滤后的菜单项
├── directives/perm.ts   # v-perm 按钮级权限
├── stores/
│   ├── auth.ts          # 登录态（令牌只放内存）
│   └── toast.ts         # 全局提示
├── layouts/AdminLayout.vue
├── pages/               # login / dashboard / 四个业务页占位 / forbidden / not-found
├── router/index.ts      # 路由 meta.permission + 导航守卫
└── types/api.ts         # 与 server/model/response.py 对齐的类型
```

## 权限模型

**角色级别**：总裁(100) > 开发者(90) > 审计(60) > 部门负责人(40) > 员工(10)。
账号操作遵循「只能管级别严格低于自己的」——所以总裁能停用开发者，开发者之间互相动不了。
账号列表里的 `manageable` 字段由后端算好，前端直接用，不自己推。

登录与会话恢复响应会携带服务端计算的 `permissions`。**功能权限**三层控制，缺一不可：

| 层 | 位置 | 作用 |
| -- | ---- | ---- |
| 路由 | `router/index.ts` 的 `meta.permission` + 守卫 | 无权限跳 `/forbidden` |
| 菜单 | `composables/useMenus.ts` | 与路由共用判定，不会「菜单看得见、点进去被踢」 |
| 按钮 | `v-perm` 指令 / `can()` | 单个操作的显隐或置灰 |

**`audit:view` 是按部门判定的，不看角色**（决策 #8「仅管理部门可见」）。这条规则只在
服务端计算，前端直接消费最终权限点，不再复制一套部门判定逻辑作为当前会话的权限真源。

```vue
<v-btn v-perm="'dept:edit'">新建部门</v-btn>
<v-btn v-perm.disable="'dept:edit'">新建部门</v-btn>  <!-- 置灰而非隐藏 -->
```

⚠️ **前端权限只决定界面显示，不是安全边界。** 真正的拦截在网关
`utils/dependencies.py::require_permission`。服务端 `services/permissions.py` 是唯一权限真源；
`constants/roles.ts` 中的本地映射仅用于兼容尚未下发 `permissions` 的旧版后端。

## 登录态处理

- 登录一次返回 token、当前用户、权限点和默认页面，不依赖第二次 `/info` 请求才能进入后台
- `access_token` 只放内存，不写 localStorage —— 后台能查全员会话审计，令牌被 XSS 拿走代价太大
- 刷新页面靠同源 httpOnly Cookie 恢复会话（`GET /admin/user/info`）
- 收到 10002 自动刷新令牌并重放请求，并发请求共用一次刷新（单飞）
- 10002 重放仍失败、10003 或账号禁用 → 清状态跳登录页
- 默认有效期：access token 60 分钟、refresh token 7 天；Cookie 有效期跟随服务端配置

## 常用命令

```bash
npm run dev         # 开发服务器
npm test            # Vitest 登录态/权限/代理回归测试
npm run type-check  # vue-tsc 类型检查（含 .vue 模板）
npm run lint        # eslint（eslint-config-vuetify）
npm run build       # 生产构建
```

## 待办

- [ ] 会话审计页（后端未实现）
- [ ] 用户列表服务端分页（当前一次拉 200 条）
- [ ] 部门树形层级展示（数据结构已支持 parent_id，界面还是平铺）
- [ ] 模型停用的二次确认改用对话框（当前用的浏览器 confirm）
