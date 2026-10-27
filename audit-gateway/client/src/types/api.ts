/**
 * 与网关 model/response.py 对齐的类型定义
 * 改后端响应结构时，这里要同步改
 */

/** 统一响应信封 */
export interface ApiEnvelope<T = unknown> {
  code: number
  message: string
  data: T
}

/** 错误码 —— 取自 server/model/response.py 的 code_map */
export const API_CODE = {
  OK: 0,
  UNKNOWN: -1,
  BAD_CREDENTIALS: 10_001,
  ACCESS_TOKEN_INVALID: 10_002,
  REFRESH_TOKEN_INVALID: 10_003,
  FIELD_TOO_LONG: 10_004,
  NAME_EXISTS: 10_005,
  MISSING_FIELD: 10_006,
  ACCOUNT_DISABLED: 10_007,
  NO_PERMISSION: 10_008,
  INVALID_OPERATION: 10_009,
  DATA_ERROR: 10_010,
} as const

/** 登录返回 */
export interface LoginInfo {
  token: string
  access_token: string
  refresh_token: string
  token_type: string
  user: UserInfo
}

/** 当前用户信息 */
export interface UserInfo {
  id: number
  uid: string
  username: string | null
  nickname: string | null
  realname: string | null
  status: number
  permission: string
  dept_id: number | null
  dept_name: string | null
  /** 部门编码 —— 会话审计可见性按部门判定，所以登录态里必须带 */
  dept_code: string | null
  expired: number | null
  /** 服务端计算后的权限点，前端菜单/路由/按钮统一以它为准 */
  permissions: string[]
  /** 登录后没有指定 redirect 时进入的路由名称 */
  default_route: string
  must_change_password?: boolean
}

/** 列表类接口的统一外层 */
export interface ListResult<T> {
  items: T[]
}

/** 部门 */
export interface Department {
  dingtalk_dept_id?: string | null
  id: number
  code: string
  name: string
  parent_id: number | null
  status: number
  remark: string | null
}

/** 部门成员（部门详情里的精简结构） */
export interface DepartmentMember {
  uid: string
  username: string | null
  realname: string | null
  permission: string
  status: number
}

/** 账号列表项 */
export interface Account {
  uid: string
  username: string | null
  realname: string | null
  nickname: string | null
  permission: string
  dept_id: number | null
  status: number
  last_login_at: string | null
  /** usage_records 中最近一次 call_kind=chat 的真实请求时间 */
  last_request_at: string | null
  /** 后端算好的「当前操作者能否管理这个账号」，前端不必自己推 */
  manageable: boolean
}

/** 当前操作者的权限上下文 */
export interface AccountContext {
  role: string
  role_level: number
  dept_code: string | null
  permissions: string[]
  assignable_roles: string[]
}

/** 供客户端调用网关的个人 API Key（明文永不出现在列表接口） */
export type ApiKeyState = 'active' | 'revoked' | 'expired' | 'idle'

export interface ApiKeyRecord {
  id: number
  name: string
  key_prefix: string
  status: number
  /** 服务端算好的状态，与网关认证的拒绝顺序一致：吊销 > 过期 > 闲置 > 有效 */
  state?: ApiKeyState
  scope?: string
  expires_at: string | null
  /** 照现在的使用情况，哪天会闲置失效；不参与闲置判定时为空 */
  idle_expires_at?: string | null
  total_token_limit: number | null
  used_tokens: number
  last_used_at: string | null
  created_at: string | null
  revoked_at?: string | null
  revoke_reason?: string | null
  revoked_by?: string | null
}

export interface ApiKeyOverviewRecord extends ApiKeyRecord {
  owner: { uid: string, name: string, username: string | null, dept_id: number | null, status: number | null }
}

/** API Key 创建结果；api_key 明文只在本次响应中出现 */
export interface ApiKeyCreateResult {
  id: number
  api_key: string
  key_prefix: string
}

/** 每日 token 配额；按用户/部门 + 模型生效 */
export interface TokenQuotaRule {
  id: number
  scope: 'user' | 'dept'
  scope_value: string
  scope_name: string
  model_id: number
  model_name: string
  daily_token_limit: number
  used_tokens: number
  remaining_tokens: number
  exceeded: boolean
  status: number
  remark: string | null
}

/** 一条已发布的桌面端版本清单 */
export interface ClientRelease {
  id: number
  version: string
  platform: string
  arch: string
  policy: 'optional' | 'required'
  min_supported_version: string
  artifact_url: string
  artifact_sha256: string
  artifact_size: number
  release_notes: string
  released_at: string
  public_key_fp: string
  status: number
  published_by: string
  created_at: string | null
}

export interface ReleaseListResult {
  items: ClientRelease[]
  /** 网关当前配的签名公钥指纹；与客户端内置的那把不一致时谁也装不上 */
  gateway_public_key_fp: string | null
}

/** 客户端可用模型 */
export interface ModelPublicInfo {
  id: string
  display_name: string
  supports_stream: boolean
  supports_multimodal: boolean
  max_context_tokens: number | null
}

/** 管理端模型信息 */
export interface ModelAdminInfo {
  capability?: 'chat' | 'image'
  id: number
  provider_id: number
  provider_code: string | null
  model_name: string
  display_name: string
  alias: string | null
  supports_stream: boolean
  supports_multimodal: boolean
  max_context_tokens: number | null
  /** 计价模式：token（按 token）/ per_call（按次）。分段/阶梯计价暂不支持 */
  billing_mode: string
  /** 单价 null 表示未配置 —— 报表会把这部分费用标成「无法估算」而不是 0 */
  input_price_per_1m: number | null
  output_price_per_1m: number | null
  /** 留空 = 没单独配 → 按输入单价计。**不是免费** */
  cached_input_price_per_1m: number | null
  price_per_call: number | null
  currency: string
  status: number
  remark: string | null
}

/** 上游供应商（api_key 只返回是否已配置） */
export interface ProviderInfo {
  kind?: 'openai' | 'codex'
  id: number
  code: string
  name: string
  base_url: string
  api_key_masked: string
  timeout_seconds: number
  status: number
  remark: string | null
}

/** 白名单条目 */
export interface WhitelistEntry {
  id: number
  scope: string
  scope_value: string
  model_id: number
  allowed: boolean
  remark: string | null
}

/** 白名单作用域 —— 与 server/model/database.py 的 SCOPE_* 对应 */
export const SCOPE = {
  GLOBAL: 'global',
  DEPT: 'dept',
  ROLE: 'role',
  USER: 'user',
} as const

export const SCOPE_LABELS: Record<string, string> = {
  [SCOPE.GLOBAL]: '全员',
  [SCOPE.DEPT]: '部门',
  [SCOPE.ROLE]: '角色',
  [SCOPE.USER]: '指定用户',
}

// ==================== 用量统计 ====================

/**
 * 一组指标 —— 概览、分组行、趋势点共用
 *
 * 三个字段要特别小心，渲染时不能当普通数字处理：
 * - `estimated_*`：上游没返回 usage 时按字符估算的部分，必须单独标出来
 * - `cost`：为 null 表示这组里有模型没配单价，费用算不全，显示「—」而不是 0
 * - `cost_basis`：金额是按**调用时**单价算的（snapshot）还是拿**当前**单价
 *   回算的（current，仅限单价快照上线前的历史数据），或两者混合（mixed）。
 *   回算出来的金额会随日后调价变动，界面上必须能区分，否则「上个月的报表
 *   今天打开数变了」永远解释不清
 */
export interface UsageMetrics {
  requests: number
  success_count: number
  failed_count: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  estimated_requests: number
  estimated_tokens: number
  avg_latency_ms: number
  cost: number | null
  currency: string
  /** snapshot（按调用时单价）/ current（按当前单价回算）/ mixed（两者都有） */
  cost_basis: 'snapshot' | 'current' | 'mixed'
  cost_basis_label: string
}

export interface UsageRange {
  start: string
  end: string
}

export interface UsageTrendPoint {
  bucket: string
  label: string
  metrics: UsageMetrics
}

export interface UsageSummary {
  range: UsageRange
  granularity: string
  /** 当前登录者的数据范围：all / dept / self */
  scope: string
  active_users: number
  trend_interval: 'auto' | '5m'
  total: UsageMetrics
  trend: UsageTrendPoint[]
  note: string
  /** 费用口径的补充说明。只有确实混了「按当前单价回算」的数据时才非空 */
  cost_note: string
}

export interface UsageGroupRow {
  key: string
  label: string
  metrics: UsageMetrics
}

export interface UsageBreakdownResult {
  dimension: string
  range: UsageRange
  scope: string
  items: UsageGroupRow[]
  total: UsageMetrics
  note: string
  cost_note: string
}

/** 明细行 —— 只有指标，没有聊天内容（内容走会话审计，仅管理部门可见） */
export interface UsageRecordRow {
  id: number
  request_id: string
  created_at: string | null
  user_uid: string
  username: string
  dept_id: number | null
  dept_name: string | null
  role: string | null
  model_name: string | null
  stream: boolean
  status: string
  error_code: string | null
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  cached_prompt_tokens: number
  cache_write_tokens: number
  reasoning_tokens: number
  tokens_estimated: boolean
  latency_ms: number
  /** 客户端中途断开。**与 status 正交** —— 中断的调用是成功的调用，token 照样花了 */
  aborted: boolean
  /**
   * 单价快照 —— 这一笔**当时**按什么价算的
   *
   * `cost` 为 null 表示当时没配单价（或这笔早于单价快照上线），显示「—」而不是 0。
   * 明细页存在的意义之一就是「这笔怎么这么贵」能一路追到当时的单价上。
   */
  cost: number | null
  cost_currency: string | null
  billing_mode: string | null
  input_price_per_1m: number | null
  output_price_per_1m: number | null
  cached_input_price_per_1m: number | null
  price_per_call: number | null
}

export interface UsageRecordPage {
  items: UsageRecordRow[]
  total: number
  page: number
  page_size: number
  range: UsageRange
  note: string
}

export interface UsageFilterOptions {
  departments: Array<{ id: number, name: string }>
  models: Array<{ id: number, name: string }>
  scope: string
}

/** 时间粒度 */
export const GRANULARITY_LABELS: Record<string, string> = {
  day: '按日',
  week: '按周',
  month: '按月',
}

/** 下钻维度 —— 与 server/services/usage_query.py 的 DIMENSION_* 对应 */
export const USAGE_DIMENSIONS = [
  { value: 'user', title: '按人' },
  { value: 'dept', title: '按部门' },
  { value: 'role', title: '按角色' },
  { value: 'model', title: '按模型' },
  { value: 'date', title: '按时间' },
]

/** 数据范围提示语 —— 让人一眼知道自己看到的是不是全部 */
export const USAGE_SCOPE_LABELS: Record<string, string> = {
  all: '全公司',
  dept: '仅本部门',
  self: '仅本人',
}

// ==================== 会话审计 ====================
// 对应 server/router/admin/audit.py。与用量那套的关键差异：
// 审计没有 scope —— 可见性完全落在「是否在管理部门」上，
// 进得来就看全公司，进不来一条都看不到（决策 #8、#17）。

/** 列表行 —— **不含全文**，只有摘要（全文走 detail 接口，那会精确记一条查阅日志） */
export interface AuditListRow {
  id: number
  request_id: string
  created_at: string | null
  user_uid: string
  username: string
  dept_id: number | null
  dept_name: string | null
  role: string | null
  model_name: string | null
  status: string
  /** 服务端是否兜底替换过关键词 */
  masked: boolean
  /** 内容入库时是否被截断 —— 详情页要据此提醒「你看到的不是全部」 */
  truncated: boolean
  stream: boolean
  /** 客户端中途断开，不是失败 */
  aborted: boolean
  attachment_count: number
  session_id: string | null
  /** chat / title / approval / other —— 非 chat 默认不显示 */
  turn_kind: string
  preview: string
  /** 和上一轮问的是同一句。Hermes 每轮重发完整历史，会话内这几乎总是 true ——
   *  所以会话展开时只在它为 false 时才显示问题，否则就是同一句话刷屏 */
  repeats_question: boolean
  response_preview: string
  /** 这一轮**新增**的工具调用（不是上下文里的累计）。
   *  ⚠️ null = 算不出（会话第一轮、跨页、上下文被压缩过），
   *  和 `[]`「这一轮没调工具」是两回事，界面上要长得不一样 */
  tools: Array<{ name: string, count: number, chars: number }> | null
  /** 上下文里累计有多少条工具结果 —— 「这次请求往模型里灌了多少」 */
  tools_in_context: number
}

export interface AuditListPage {
  items: AuditListRow[]
  total: number
  page: number
  page_size: number
  range: UsageRange
}

/** 附件记录。stored=false 时只有元数据，文件本体没留存（超限/关开关/远程链接） */
export interface AuditAttachment {
  id: number
  marker: string
  kind: 'image' | 'audio' | 'file' | string
  mime_type: string | null
  filename: string | null
  size_bytes: number
  size_text: string
  sha256: string | null
  /** 远程链接形态的附件：网关按设计未抓取内容，只展示不做成可点链接 */
  origin_url: string | null
  stored: boolean
  store_error: string | null
}

/** 会话列表的一行 —— 一个会话，不是一次调用（决策 #52 · C1） */
export interface AuditSessionRow {
  /** 分组键。没有 session_id 的记录用 `#<id>`，**只用于前端 key 与展开状态**，
   *  不要拿它反查会话 */
  session_key: string
  session_id: string | null
  /** false = 这条记录没有会话 ID（stock 客户端、绕过客户端的直连），
   *  退化成一行一条。⚠️ 这类记录必须照常显示，不能因为分不了组就藏起来 */
  linked: boolean
  user_uid: string
  username: string
  dept_id: number | null
  dept_name: string | null
  role: string | null
  model_name: string | null
  turns: number
  first_id: number
  last_id: number
  started_at: string | null
  ended_at: string | null
  total_tokens: number
  attachment_count: number
  has_failure: boolean
  has_masked: boolean
  has_truncated: boolean
  /** 会话内第一条记录的摘要，当标题用 */
  title: string
}

export interface AuditSessionPage {
  items: AuditSessionRow[]
  total: number
  page: number
  page_size: number
  range: { start: string, end: string }
}

export interface AuditDetail {
  id: number
  request_id: string
  created_at: string | null
  user_uid: string
  username: string
  dept_id: number | null
  dept_name: string | null
  role: string | null
  model_name: string | null
  status: string
  masked: boolean
  truncated: boolean
  stream: boolean
  aborted: boolean
  /** Hermes 会话 ID。客户端给的、可伪造，只用于把同一次会话的记录串起来看 */
  session_id: string | null
  /** chat / title / approval / other —— 非 chat 的在列表里默认隐藏 */
  turn_kind: string
  /** 本次用户输入全文（决策 #52：独占额度，不和系统提示抢） */
  user_input: string
  user_input_markers: string[]
  /** 接收方向只存不改（决策 #9）：这就是模型真实说过的话 */
  response_text: string
  response_markers: string[]
  /** 系统提示词**只有指纹**。它能回答的唯一问题是「是不是同一版提示词」，
   *  不要在界面上暗示还能看到正文 */
  system_prompt_sha256: string | null
  system_prompt_chars: number
  /** 本地工具链只有骨架：哪个工具、各多长。⚠️ 正文一个字不存 ——
   *  这是知情取舍，「什么公司数据被发给了模型」只剩体量不剩内容 */
  tools: Array<{ name: string, chars: number }>
  /** 按工具名汇总，字数降序 —— 详情页默认展示这个而不是逐次明细：
   *  一次 agent 会话能有 63 次工具调用，全摊开是一面读不出东西的墙 */
  tool_summary: Array<{ name: string, count: number, chars: number }>
  tool_chars_total: number
  attachments: AuditAttachment[]
}

export interface AuditFilterOptions {
  departments: Array<{ id: number, name: string }>
  models: Array<{ id: number, name: string }>
  /** 列的是全部在职用户，不只是「有会话记录的」——
   *  「查了但没有记录」和「压根查不了」是两个完全不同的审计结论 */
  users: Array<{
    uid: string
    name: string
    dept_id: number | null
    dept_name: string | null
    role: string | null
  }>
}

/** 查阅日志行 —— 「看的人也被看」 */
export interface AuditAccessLogRow {
  id: number
  created_at: string | null
  actor_uid: string
  actor_name: string
  actor_role: string | null
  action: string
  action_label: string
  target_conversation_id: number | null
  target_attachment_id: number | null
  target_user_uid: string | null
  target_name: string | null
  query_json: string | null
  result_count: number
  client_ip: string | null
}

export interface AuditAccessLogPage {
  items: AuditAccessLogRow[]
  total: number
  page: number
  page_size: number
  range: UsageRange
}

/** 查阅动作 —— 与 server/services/audit_access.py 的 ACTION_* 对应 */
export const AUDIT_ACTIONS = [
  { value: 'list', title: '检索列表' },
  { value: 'detail', title: '查看全文' },
  { value: 'attachment', title: '查看附件' },
  { value: 'export', title: '导出 CSV' },
]

/** 动作标识色：越接近「内容离开系统」越警示 */
export const AUDIT_ACTION_COLORS: Record<string, string> = {
  list: 'grey',
  detail: 'primary',
  attachment: 'deep-purple',
  export: 'warning',
}

/** 附件类型图标 */
export const ATTACHMENT_ICONS: Record<string, string> = {
  image: 'mdi-image-outline',
  audio: 'mdi-music-note-outline',
  file: 'mdi-file-outline',
}

// ==================== 技能安全事件 ====================
// 对应 server/router/admin/skill_report.py（阶段 7 · S7-C，管理端只读）
//
// ⚠️ 与会话审计的两点关键差异，直接决定这几个类型长什么样：
//
// 1. **这不是拦截报表，是越过报表。** 技能来源不封禁，扫描器判 dangerous 会自动
//    隔离，但员工可以自己放回去 —— 代价是留一行。所以一条 `skill_quarantined`
//    本身不说明有人做错了什么，配对的 `skill_quarantine_restored` 才是要看的。
// 2. **这里还没有查阅日志。** 拉详情、看样本都不在后端留痕（TODO 里记着）。
//    等它补上时，任何「hover 就预取样本」的交互都会立刻变成一堆没有人真做过的
//    查阅记录 —— 所以现在就按「取代码必须是一次明确点击」来写。

/** 事件类型筛选项 */
export const SKILL_EVENTS = [
  { value: 'skill_quarantined', title: '隔离' },
  { value: 'skill_quarantine_restored', title: '员工放回' },
  { value: 'skill_publish_warning', title: '发布扫描提醒' },
]

export const SKILL_EVENT_LABELS: Record<string, string> = {
  skill_quarantined: '隔离',
  skill_quarantine_restored: '员工放回',
  skill_publish_warning: '发布扫描提醒',
}

/** 放回与发布提醒都需要管理员事后核查。 */
export const SKILL_EVENT_COLORS: Record<string, string> = {
  skill_quarantined: 'warning',
  skill_quarantine_restored: 'error',
  skill_publish_warning: 'warning',
}

/** 扫描结论（tools/skills_guard.py 的 verdict） */
export const SKILL_VERDICTS = [
  { value: 'dangerous', title: 'dangerous' },
  { value: 'caution', title: 'caution' },
  { value: 'safe', title: 'safe（有提示项）' },
]

export const SKILL_VERDICT_COLORS: Record<string, string> = {
  dangerous: 'error',
  caution: 'warning',
  safe: 'success',
}

/** 命中项严重度（skills_guard.Finding.severity） */
export const SKILL_SEVERITY_COLORS: Record<string, string> = {
  critical: 'error',
  high: 'error',
  medium: 'warning',
  low: 'grey',
}

/** 列表行 —— **不含 findings，也不含任何样本内容**，见模块说明 */
export interface SkillEventRow {
  id: number
  event: string
  user_uid: string
  dept_id: number | null
  dept_code: string | null
  /** 客户端自报的机器标签，仅供人工定位「去哪台机器上看」，不参与判定 */
  device: string | null
  skill_name: string
  verdict: string | null
  source: string | null
  /** 结论随扫描器版本变：同一个技能换个版本可能就不隔离了 */
  scanner_version: string | null
  /** 同一技能包的隔离、放回与发布提醒靠它串联 */
  bundle_hash: string | null
  finding_count: number
  has_sample: boolean
  /** 客户端 UTC 原文字符串，机器时间被改过就会是假的。排序一律用 created_at */
  occurred_at: string | null
  /** 网关收到的时间，全库统一口径 */
  created_at: string | null
}

export interface SkillEventPage {
  items: SkillEventRow[]
  total: number
  page: number
  page_size: number
}

/** 一条命中项 —— 对应 tools/skills_guard.py 的 Finding */
export interface SkillFinding {
  pattern_id?: string
  severity?: string
  category?: string
  file?: string
  line?: number
  /** 命中那一行的原文，超过 120 字符时客户端已经截断过 */
  match?: string
  description?: string
}

export interface SkillEventDetail extends Omit<SkillEventRow, 'has_sample'> {
  event_id: string
  entry_id: string | null
  reason: string | null
  origin_relpath: string | null
  findings: SkillFinding[]
  /** 为空表示这条事件没带样本 —— 放回事件本来就不带（隔离时已经传过） */
  sample_id: number | null
  reported_at: string | null
  /** 同一个 bundle_hash 的其它事件，用来回答「有多少人把它放回去了」 */
  related: SkillEventRow[]
}

/** 样本里的一个文件。`included=false` 时没有 content，只有元数据 */
export interface SkillSampleFile {
  path: string
  size_bytes?: number
  sha256?: string
  included: boolean
  content?: string
  /** too_large / binary / budget_exhausted */
  omitted?: string
  /** 读不出来时的原因，此时连 size 都没有 */
  error?: string
}

/** 样本本体 —— 员工自己写的源码，要 skill_security:sample 才拿得到 */
export interface SkillSample {
  id: number
  sample_hash: string
  bundle_hash: string | null
  file_count: number
  included_count: number
  included_bytes: number
  /**
   * 有文件因超限或是二进制没传上来。
   * ⚠️ 界面上必须显眼：漏了它，读到一份看着人畜无害的样本就会得出「误报」的
   * 结论，而真正的东西在那个没传上来的 12MB 文件里。
   */
  truncated: boolean
  truncated_reason: string | null
  files: SkillSampleFile[]
  /** 这份样本被上报过多少次 —— 按内容去重，所以它约等于「扩散到了几台机器」 */
  seen_count: number
  first_seen_at: string | null
  last_seen_at: string | null
}

// ==================== 自有 skill hub（阶段 11 · S11-G）====================

/** 后台技能列表的一行。**不含文件内容** —— 见 api/skill-hub.ts 的说明 */
export interface SkillHubRow {
  id: number
  /** dept=部门空间 / company=公司空间。没有 personal——个人技能留在客户端本地 */
  scope: string
  /** 所属部门空间；company 时为 null */
  scope_dept_id: number | null
  /** 安装标识，也是技能落到磁盘上的目录名 */
  slug: string
  name: string
  description: string
  /** 当前版本号。服务端递增的整数，不是 semver */
  version: number
  owner_uid: string
  owner_username: string
  owner_dept_code: string
  owner_dept_name: string
  /** 取包次数，不是安装成功数 —— 客户端取完还要过扫描与安装策略 */
  install_count: number
  /** listed / delisted */
  status: string
  /** 员工来问「我搜不到 xxx 了」时，答案在这里 */
  delisted_reason: string
  delisted_at: string
  created_at: string
  updated_at: string
}

export interface SkillHubPage {
  items: SkillHubRow[]
  total: number
  page: number
}

/** 一个技能的某一版。同样不含文件内容 */
export interface SkillHubVersion {
  version: number
  /** 服务端按「路径 + 各文件 sha256」算的内容指纹 */
  bundle_hash: string
  file_count: number
  total_bytes: number
  changelog: string
  publisher_username: string
  publisher_dept_code: string
  publisher_dept_name: string
  created_at: string
}

/** 技能包里的一个文件。发布的包是完整的，所以没有样本那套 included/omitted */
export interface SkillHubFile {
  path: string
  /** utf-8 / base64。base64 的是二进制附件，界面不展开内容 */
  encoding: string
  content: string
}

/**
 * 一个技能包某一版的**内容本体** —— 后台审阅用（2026-09-03）。
 *
 * 与技能安全的样本（SkillSample）有一点关键不同：那边是**被隔离的**代码，未必
 * 等于任何人机器上真正跑着的东西，而且可能因为超限只传上来一半；这里是**已发布**
 * 的包本体，就是同事装下去会执行的那一份，bundle_hash 由服务端算，一定完整。
 */
export interface SkillHubFiles {
  slug: string
  name: string
  status: string
  scope: string
  version: number
  bundle_hash: string
  file_count: number
  total_bytes: number
  changelog: string
  publisher_username: string
  published_at: string
  files: SkillHubFile[]
}

/**
 * 技能指标。
 *
 * skill_count 与 version_count 都给，是因为「把一个技能迭代五版」也是五次总结
 * 工作经验，只数技能会让持续打磨的人看起来不如到处新建的人。按哪个考核是管理
 * 决策，接口不替人定死。
 *
 * ⚠️ 两个数都是**累计**，不是本月。按月切分要先定「月」的口径，而报表口径是要
 * 被拿去考核的东西，定错了比没有更糟。
 */
export interface SkillHubDeptStat {
  dept_id: number | null
  dept_code: string
  dept_name: string
  skill_count: number
  version_count: number
}

export interface SkillHubPublisherStat {
  uid: string
  username: string
  dept_code: string
  dept_name: string
  skill_count: number
  version_count: number
}

export interface SkillHubStats {
  by_dept: SkillHubDeptStat[]
  by_publisher: SkillHubPublisherStat[]
}

// ==================== 协作（项目群聊）—— 阶段 17 ====================
//
// ⚠️ 这几个接口是 `/v1/*` 不是 `/admin/*`。
//
//   它们是给**客户端**（桌面端 sidecar）设计的，用 API Key 认证。后台页能直接
//   调，是因为网关的 require_caller 同时接受 JWT —— 而 http.ts 带
//   `credentials: 'include'`，cookie 里的 access_token 就是那把 JWT
//   （见 server/utils/gateway_auth.py 的模块说明：「两条通道对应两种客户端」）。
//
//   所以这里**没有**、也不需要一套 /admin/projects 端点。多开一套只会带来
//   两份权限判定，而它们会各自漂移。

export interface ProjectRow {
  id: number
  /** 项目标识，创建后不可改（改了等于换项目） */
  code: string
  name: string
  description: string
  owner_uid: string
  /** active / archived */
  status: string
  /** 自建 Gitea 仓库；ready 前为空 */
  git_repo_url: string
  git_repo_id: number | null
  git_repo_name: string
  /** pending / provisioning / failed / ready */
  git_repo_status: string
  git_repo_error: string
  created_at: string
  updated_at: string
  /** 只在「我的项目」列表里有：owner / member / viewer */
  my_role?: string
}

export interface ProjectMember {
  id: number
  /** user / agent。agent 是一等成员，不是人的一个布尔字段 */
  member_kind: string
  /** 人成员=本人；**agent 成员=主人的 uid**（归属与记账都靠它） */
  user_uid: string
  username: string
  agent_label: string
  dept_code: string
  role: string
  status: string
  joined_at: string
}

export interface ProjectMessage {
  id: number
  /** 项目内序号。读游标用它，不用全局 id（后者会因别的项目的写入而跳号） */
  seq: number
  sender_kind: string
  sender_uid: string
  sender_username: string
  sender_agent_label: string
  sender_dept_code: string
  reply_to_id: number | null
  /** **脱敏后**的正文。原文不下发，只走审计侧 */
  content: string
  /** 服务端替换过关键词。收到的人有权知道自己看到的不是原话 */
  redacted: boolean
  created_at: string
}

export interface ProjectTask {
  id: number
  seq: number
  title: string
  body: string
  /** open / claimed / active / done / cancelled。claimed ≠ active，见 P0-6 */
  status: string
  /** chat=来自群聊 / manual=成员手写 */
  source_kind: string
  source_message_id: number | null
  /**
   * 不可信标记。**界面必须把它显示出来** —— 漏掉这个字段，「群里来的」和
   * 「自己写的」在界面上长得一模一样，而认领人在确认前看到的提醒是 P0-6
   * 整条防线唯一的人在环环节。
   */
  untrusted: boolean
  created_by_uid: string
  created_by_username: string
  claimed_by_uid: string
  claimed_by_username: string
  claimed_agent_label: string
  claimed_at: string
  confirmed_at: string
  /** 被退回未认领池几次。退过三次的多半是描述有问题 */
  returned_count: number
  created_at: string
}

/** 认领后必须原样呈给人看的东西 —— P0-6 的人在环 */
export interface TaskConfirmation {
  title: string
  /** **原文原样，不摘要不高亮**。摘要会把注入的那句话摘掉，而它恰恰是要让人看见的 */
  body: string
  source_kind: string
  untrusted: boolean
  /** 只有 untrusted 时才有。措辞是陈述事实，不喊「这可能是攻击」 */
  reminder?: string
}

export interface ClaimResult {
  claimed: boolean
  status: string
  confirmation: TaskConfirmation
  next: string
}

/** 屏蔽词规则（阶段 18）。形状照配额/白名单：scope + scope_value */
export interface RedactionRule {
  id: number
  /** global / dept */
  scope: string
  /** 部门ID；global 为空串 */
  scope_value: string
  keyword: string
  status: number
  remark: string
  updated_at: string
}
