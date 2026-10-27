/**
 * CUSTOM: 斜杠命令弹层的中文化查表。
 *
 * ## 为什么翻译放在渲染层
 *
 * 命令表有两个真源，都是英文：sidecar 的 `hermes_cli/commands.py`（95 条，
 * **上游文件**）和 `desktop-slash-commands.ts`（35 条，我们自己的）。直接去改
 * 描述文案的代价是每次同步上游都要在 95 行里解冲突 —— 正是「定制纪律：同步冲突量
 * ∝ 修改上游代码的行数」要避开的事。
 *
 * 所以数据层原样保留英文，只在弹层渲染时按命令名查一次表。好处是语言切换立即生效
 * （走 `useI18n()` 的响应式 `t`），而不是等命令缓存过期。
 *
 * ## 查不到就回落
 *
 * 这张表**不需要**跟命令表保持同步。上游新增一条命令时，它只是暂时显示英文原文，
 * 不会变成空白、也不会把 i18n key 漏到界面上 —— 后者是用 `translateNow()` 做这件事
 * 会有的失败模式（它查不到时返回 key 本身）。
 */

import { TRANSLATIONS } from '@/i18n/catalog'
import { getRuntimeI18nLocale } from '@/i18n/runtime'

/** 当前语言下的两张表。
 *
 * 弹层走 `useI18n()` 的响应式 `t`（切语言要立刻重画），而 `/help` 那种**一次性
 * 生成进转录**的文本没有重画这回事，用运行时语言即可 —— 与 `translateNow()`
 * 同一套取法。两个入口共用下面的查表函数，只是表的来源不同。
 */
function runtimeTables(): { descs: Record<string, string>; groups: Record<string, string> } {
  const composer = TRANSLATIONS[getRuntimeI18nLocale()]?.composer

  return { descs: composer?.slashCommandDescs ?? {}, groups: composer?.slashGroups ?? {} }
}

/** 从一条补全项里取出可用于查表的命令名。
 *
 * 弹层里的 `display` 可能带参数（`/skin list`、`/pet boba`）或是 `@` 引用，
 * 所以只取第一个 token，并且只认以 `/` 开头的。大小写按原样 —— 命令名本来就是小写，
 * 强行 lower 反而会让 `/Foo` 这种后端真发出来的怪名字查不到它自己。
 */
export function slashCommandKey(display: string): null | string {
  const token = (display || '').trim().split(/\s+/, 1)[0]

  return token.startsWith('/') && token.length > 1 ? token : null
}

/**
 * 命令说明的本地化文案，查不到返回 `fallback`（后端给的英文）。
 *
 * `fallback` 为空时返回空串 —— 调用方据此决定整行不渲染说明，而不是渲染一个空位。
 */
export function localizeSlashDescription(
  display: string,
  fallback: string,
  table?: Record<string, string>
): string {
  const key = slashCommandKey(display)
  const source = table ?? runtimeTables().descs

  if (!key) {
    return fallback
  }

  return source[key] || fallback
}

/** 分组表头的本地化文案，查不到原样返回。 */
export function localizeSlashGroup(group: string, table?: Record<string, string>): string {
  if (!group) {
    return group
  }

  return (table ?? runtimeTables().groups)[group] || group
}
