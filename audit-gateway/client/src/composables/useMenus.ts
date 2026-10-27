/**
 * 侧边菜单项 —— 按当前角色过滤
 *
 * 与路由守卫共用 roleHasPermission，保证「菜单里有的一定进得去」。
 * 顺序显式声明，不依赖 router.getRoutes() 的返回顺序。
 */

import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { roleHasPermission } from '@/constants/roles'
import { useAuthStore } from '@/stores/auth'

export interface MenuItem {
  name: string
  title: string
  icon: string
}

/**
 * 菜单顺序 —— **同时也是白名单**
 *
 * ⚠️ 在路由上写 `meta.menu: true` 不足以让一个页面出现在侧边栏，
 * 名字还必须列进这个数组。少列一个的表现是「页面在、URL 直接敲进去能打开、
 * 菜单里没有」—— 没有任何报错，看着像权限被拦了。
 *
 * 这个坑已经踩过两次：`releases`（桌面端版本）从加上那天起就不在菜单里，
 * `skill-events`（技能安全）2026-08-27 重蹈覆辙。所以补了一条用例把
 * 「路由声明了 menu 就必须在这里」钉死，见 useMenus.test.ts。
 *
 * 顺序是显式的，不跟着 router.getRoutes() 走 —— 那个顺序不保证稳定。
 */
/**
 * 菜单分组。
 *
 * ⚠️ 这仍然是一份**手写白名单**：路由上写 `meta.menu: true` 不够，名字必须同时
 * 出现在某个分组的 `items` 里。少列一个的表现是「页面在、URL 敲得进去、菜单里
 * 没有」，没有任何报错——这个坑踩过三次（releases、skill-events、collab），
 * 由 useMenus.test.ts 钉住。
 *
 * ## 为什么改成分组
 *
 * 2026-08-31 加到 11 项之后侧栏出现了垂直滚动条。滚动本身不是问题，问题是
 * **平铺 11 项之后，找一个页面要从头读到尾** —— 而这些页面的使用频率差着数量级：
 * 「协作」是每天开的，「部门」是一年改两次的。
 *
 * ## 分组按「谁来用、多久用一次」，不按「功能像不像」
 *
 * - **日常**：普通员工每天会点的。放最上面，不折叠
 * - **运营**：主管/审计每周看的报表与记录
 * - **配置**：改完就走的系统设置，一年动几次
 *
 * 按功能归类（比如把「技能库」和「技能安全」放一起）看起来整齐，但那两个页面的
 * 使用者完全不同：前者是全体员工找技能，后者是安全复核。摆在一起只会让员工每次
 * 都要跳过一个他打不开的入口。
 */
export interface MenuGroup {
  title: string
  items: string[]
}

export const MENU_GROUPS: MenuGroup[] = [
  {
    title: '日常',
    items: [
      'dashboard',
      // 协作是普通员工唯一日常会用的页面，排在最前
      'collab',
      'skill-hub',
      'sites',
      'apikeys',
    ],
  },
  {
    title: '运营',
    items: [
      'usage',
      // 技能安全紧挨着会话审计：两个都是「事后翻记录」的页面，
      // 找的人通常分不清要去哪一个，摆在一起比按功能归类好用。
      'skill-events',
      'audit',
    ],
  },
  {
    title: '配置',
    items: [
      'departments',
      'users',
      'sso-clients',
      'models',
      'quotas',
      'mcp',
      // 屏蔽词是安全配置：删一个词等于当场关掉一条防线
      'redaction',
      'prompt-policy',
      'releases',
    ],
  },
]

/** 扁平顺序。给 useMenus.test.ts 那条「路由声明了 menu 就必须被列出」用 */
export const MENU_ORDER = MENU_GROUPS.flatMap(g => g.items)

/**
 * 做了一半、暂时不开放的功能。
 *
 * ## 为什么保留这个空集合，而不是把机制删掉
 *
 * 删掉的话，下一次要藏一个半成品入口时，做法会退回「把菜单项删掉」——
 * 而那个做法的表现是「页面在、URL 敲得进去、菜单里没有」，看起来和 MENU_ORDER
 * 漏列一模一样（这个坑本项目踩过三次）。留着它，藏和恢复都是一行。
 *
 * ## 协作为什么从这里拿掉了（2026-09-07）
 *
 * 当初挡住协作的理由写得很清楚：**桌面端没有界面，而且 agent 还不能在群里发言**。
 * 阶段 27 把这两条都做掉了 —— 桌面端有独立的「项目协作」页，agent 能拿租约、
 * 能在群里回复，P0 验收也跑完了。理由不成立了，闸门就该开。
 *
 * ⚠️ 顺带记一笔定位的变化：后台这一页现在是**只读看板**。建项目、发言、建任务、
 * 认领开工全部回到客户端 —— 协作发生在客户端，后台负责看。
 */
export const UNRELEASED = new Set<string>()

export function useMenus () {
  const auth = useAuthStore()
  const router = useRouter()

  /** 单个入口可见与否。权限判定与之前完全一致，只是抽出来给分组复用 */
  function visible (name: string): MenuItem | null {
    const role = auth.user?.permission ?? null
    // 审计菜单的可见性取决于部门（仅管理部门），所以这里要带上部门编码
    const deptCode = auth.user?.dept_code ?? null
    const route = router.getRoutes().find(r => r.name === name)

    if (!route?.meta.menu) {
      return null
    }
    // 未开放的功能不出现在菜单里。路由本身仍然注册着，敲 URL 进得去。
    if (UNRELEASED.has(name)) {
      return null
    }
    if (
      route.meta.permission
      && !roleHasPermission(role, route.meta.permission, deptCode, auth.user?.permissions)
    ) {
      return null
    }
    return {
      name,
      title: route.meta.title ?? '',
      icon: route.meta.icon ?? 'mdi-circle-small',
    }
  }

  /** 扁平列表。保留是因为别处（面包屑、命令面板）可能只要一串入口 */
  const menus = computed<MenuItem[]>(
    () => MENU_ORDER.map(name => visible(name)).filter((m): m is MenuItem => m !== null),
  )

  /**
   * 分组列表。**空分组不返回** —— 普通员工看不到「配置」组里的任何一项，
   * 那就不该给他一个空标题占着一行。
   */
  const groups = computed(() =>
    MENU_GROUPS
      .map(g => ({ title: g.title, items: g.items.map(name => visible(name)).filter((m): m is MenuItem => m !== null) }))
      .filter(g => g.items.length > 0),
  )

  return { menus, groups }
}
