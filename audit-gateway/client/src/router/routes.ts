/**
 * 路由表 —— 与 createRouter 分开放
 *
 * 分出来是为了能被测试直接 import：`router/index.ts` 里的 `createWebHistory()`
 * 一执行就要摸 `document`，在 node 环境的用例里 import 不进来。而恰恰有一条
 * 不变式必须靠用例钉住，见 `composables/useMenus.ts` 里 MENU_ORDER 的说明 ——
 * 「路由写了 menu: true」和「菜单里真的出现」是两件事，2026-08-27 之前
 * 桌面端版本页和技能安全页都因此在菜单里不存在。
 */

import type { Permission } from '@/constants/roles'
import type { RouteRecordRaw } from 'vue-router'
import AdminLayout from '@/layouts/AdminLayout.vue'

declare module 'vue-router' {
  interface RouteMeta {
    /** 页面标题，用于顶栏与浏览器标题 */
    title?: string
    /** 侧边菜单图标 */
    icon?: string
    /** 访问所需权限点，缺省表示登录即可 */
    permission?: Permission
    /** 是否免登录 */
    public?: boolean
    /** 是否在侧边菜单中显示 */
    menu?: boolean
  }
}

export const routes: RouteRecordRaw[] = [
  { path: '/change-initial-password', name: 'change-initial-password', component: () => import('@/pages/change-initial-password.vue'), meta: { title: '修改初始密码' } },
  {
    path: '/login',
    name: 'login',
    component: () => import('@/pages/login.vue'),
    meta: { title: '登录', public: true },
  },
  {
    path: '/',
    component: AdminLayout,
    children: [
      {
        path: '',
        name: 'dashboard',
        component: () => import('@/pages/dashboard.vue'),
        meta: { title: '概览', icon: 'mdi-view-dashboard-outline', menu: true },
      },
      {
        path: 'departments',
        name: 'departments',
        component: () => import('@/pages/departments.vue'),
        meta: {
          title: '部门管理',
          icon: 'mdi-sitemap-outline',
          permission: 'dept:view',
          menu: true,
        },
      },
      {
        path: 'users',
        name: 'users',
        component: () => import('@/pages/users.vue'),
        meta: {
          title: '用户与角色',
          icon: 'mdi-account-group-outline',
          permission: 'user:view',
          menu: true,
        },
      },
      {
        path: 'apikeys',
        name: 'apikeys',
        component: () => import('@/pages/apikeys.vue'),
        meta: {
          title: 'API 密钥',
          icon: 'mdi-key-chain-variant',
          menu: true,
        },
      },
      {
        path: 'models',
        name: 'models',
        component: () => import('@/pages/models.vue'),
        meta: {
          title: '模型与白名单',
          icon: 'mdi-robot-outline',
          permission: 'model:view',
          menu: true,
        },
      },
      {
        path: 'image-channels',
        name: 'image-channels',
        redirect: { path: '/models', query: { tab: 'images' } },
        meta: { permission: 'model:view' },
      },
      {
        path: 'codex-channels',
        name: 'codex-channels',
        redirect: { path: '/models', query: { tab: 'codex' } },
        meta: { permission: 'model:view' },
      },
      {
        path: 'quotas',
        name: 'quotas',
        component: () => import('@/pages/quotas.vue'),
        meta: {
          title: '限流与配额',
          icon: 'mdi-speedometer-slow',
          permission: 'model:view',
          menu: true,
        },
      },
      {
        path: 'mcp',
        name: 'mcp',
        component: () => import('@/pages/mcp.vue'),
        meta: {
          title: 'MCP 管理',
          icon: 'mdi-power-plug-outline',
          permission: 'mcp:view',
          menu: true,
        },
      },
      {
        path: 'usage',
        name: 'usage',
        component: () => import('@/pages/usage.vue'),
        meta: {
          title: '用量统计',
          icon: 'mdi-chart-timeline-variant',
          // 能进页面即可；看得到谁的数据由后端 resolve_usage_scope 决定
          permission: 'usage:view',
          menu: true,
        },
      },
      {
        path: 'releases',
        name: 'releases',
        component: () => import('@/pages/releases.vue'),
        meta: {
          title: '桌面端版本',
          icon: 'mdi-package-variant-closed',
          permission: 'release:view',
          menu: true,
        },
      },
      {
        path: 'redaction',
        name: 'redaction',
        component: () => import('@/pages/redaction.vue'),
        meta: {
          title: '屏蔽词',
          icon: 'mdi-eye-off-outline',
          // 与 model:edit 同档：改屏蔽词看着像内容配置，实际是安全配置 ——
          // 删一个词等于当场把那条防线关掉，且已发出的消息不追溯遮蔽。
          permission: 'redaction:edit',
          menu: true,
        },
      },
      {
        path: 'prompt-policy',
        name: 'prompt-policy',
        component: () => import('@/pages/prompt-policy.vue'),
        meta: {
          title: '提示词策略',
          icon: 'mdi-text-box-check-outline',
          permission: 'prompt_policy:view',
          menu: true,
        },
      },
      {
        path: 'skill-events',
        name: 'skill-events',
        component: () => import('@/pages/skill-events.vue'),
        meta: {
          title: '技能安全',
          icon: 'mdi-shield-bug-outline',
          // 只要 :view。样本源码那一层由页面内部按 skill_security:sample 再判一次 ——
          // 挂在路由上会把管理部门审计整个挡在门外，而列表与命中项本来就该给他们看。
          permission: 'skill_security:view',
          menu: true,
        },
      },
      {
        path: 'collab',
        name: 'collab',
        component: () => import('@/pages/collab.vue'),
        meta: {
          title: '协作',
          icon: 'mdi-account-group-outline',
          // **刻意不设 permission**：协作是给全体员工的，而「谁能看哪个项目」由
          // 服务端按成员判定 —— 不是成员就拿不到数据，且「不是成员」与「项目不
          // 存在」回同一个 404。前端再加一道角色判断只会造成两份判定，而它们会漂。
          menu: true,
        },
      },
      {
        path: 'skill-hub',
        name: 'skill-hub',
        component: () => import('@/pages/skill-hub.vue'),
        meta: {
          title: '技能库',
          icon: 'mdi-toolbox-outline',
          // 只要 :view。下架/恢复那两个按钮由页面内部按 skill_hub:edit 再判一次 ——
          // 挂在路由上会把只看指标的人挡在门外，而技能指标本来就该给他们看。
          permission: 'skill_hub:view',
          menu: true,
        },
      },
      {
        path: 'sites',
        name: 'sites',
        component: () => import('@/pages/sites.vue'),
        meta: { title: '网站产物', icon: 'mdi-web', menu: true },
      },
      {
        path: 'sso-clients',
        name: 'sso-clients',
        component: () => import('@/pages/sso-clients.vue'),
        meta: { title: '统一登录应用', icon: 'mdi-shield-account-outline', permission: 'user:view', menu: true },
      },
      {
        path: 'audit',
        name: 'audit',
        component: () => import('@/pages/audit.vue'),
        meta: {
          title: '会话审计',
          icon: 'mdi-file-search-outline',
          permission: 'audit:view',
          menu: true,
        },
      },
      {
        path: 'forbidden',
        name: 'forbidden',
        component: () => import('@/pages/forbidden.vue'),
        meta: { title: '无权访问' },
      },
    ],
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/pages/not-found.vue'),
    meta: { title: '页面不存在', public: true },
  },
]
