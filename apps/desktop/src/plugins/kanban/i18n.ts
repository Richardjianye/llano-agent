/**
 * Plugin-scoped i18n for kanban — bundles shipped under the plugin id via
 * ctx.i18n.register (#67303), never touching core en.ts. usePluginI18n('kanban')
 * returns a stringly-typed t(key, …); `useKanban()` binds it to the message
 * SHAPE so components keep typed `k.newTask` / `k.moveTo(label)` access.
 */

import { type PluginLocaleBundles, type PluginTranslate, usePluginI18n } from '@hermes/plugin-sdk'
import { useMemo } from 'react'

type KanbanMessages = {
  nav: string
  openBoard: string
  /** Command label — shows in the ⌘K palette AND as the keybind panel row,
   *  so it carries the "Kanban: " prefix the palette convention wants. */
  newTaskCommand: string
  countTip: (running: number, ready: number) => string
  col: Record<
    'archived' | 'blocked' | 'done' | 'ready' | 'review' | 'running' | 'scheduled' | 'todo' | 'triage',
    { label: string; help: string }
  >
  locked: { review: string; running: string; scheduled: string }
  arcRunning: string
  arcStale: string
  title: string
  orchestrationSettings: string
  newTask: string
  filterCards: string
  noMatch: string
  noTasks: string
  open: string
  select: string
  deselect: string
  moveTo: (label: string) => string
  delete: string
  reviewChecking: string
  attachedTip: (name: string) => string
  orchestratorTip: (name: string) => string
  autoAssignTip: (name: string) => string
  wontRun: string
  wontRunTip: string
  noHeartbeat: string
  expand: (label: string) => string
  collapse: (label: string) => string
  newTaskIn: (label: string) => string
  empty: string
  unassigned: string
  filters: string
  allProfiles: string
  allTenants: string
  showArchived: string
  groupRunning: string
  nSelected: (n: number) => string
  moveToShort: string
  assign: string
  unassignAction: string
  archive: string
  clearSelection: string
  refused: string
  bulkFailed: (failed: number, total: number, err: string) => string
  titlePlaceholderTriage: string
  titlePlaceholder: string
  descPlaceholder: string
  priority: string
  workspace: string
  boardDefaultSuffix: string
  workspaceOverride: string
  model: string
  modelInherit: string
  modelClear: string
  modelHint: string
  workspaceInherit: string
  workspaceInheritDir: (dir: string) => string
  workspaceInheritGeneric: string
  assignee: string
  defaultOption: (name: string) => string
  parkedOption: string
  skills: string
  skillsPlaceholder: string
  parent: string
  noParent: string
  goalMode: string
  creating: string
  createTask: string
  cancel: string
  save: string
  estimate: string
  estimateEffort: string
  estimating: string
  reEstimate: string
  makesModelCall: string
  estimateTip: string
  estimateTipLong: string
  roughEstimate: string
  tokUnit: string
  couldNotEstimate: string
  complexity: Record<'L' | 'M' | 'S', string>
  introBody: string
  introGotIt: string
  // drawer — activity prose
  evtCreated: (where: string, assignee: string) => string
  evtMovedTo: (col: string) => string
  evtParentReopened: (parent: string) => string
  evtAssignedTo: (assignee: string) => string
  evtUnassigned: string
  evtCommentBy: (author: string) => string
  evtClaimedReview: string
  evtClaimedWorker: string
  evtWorkerStarted: string
  evtCompleted: string
  evtBlocked: string
  evtUnblocked: (col: string) => string
  evtReclaimed: string
  evtSpecified: string
  evtPromoted: string
  evtScheduled: string
  evtArchived: string
  evtReprioritized: (priority: string) => string
  someone: string
  // drawer — meta + sections
  metaPriority: string
  metaTenant: string
  metaCreatedBy: string
  metaCreated: string
  metaWorkerPid: string
  readyUnassignedTitle: string
  readyUnassignedBody: string
  diagnosticsN: (n: number) => string
  commandCopied: string
  description: string
  editDescription: string
  cancelEdit: string
  noDescription: string
  result: string
  latestSummary: string
  dependencies: string
  blockedBy: string
  blocks: string
  comments: (n: number) => string
  commentsHelpRunning: string
  commentsHelp: string
  send: string
  comment: string
  messageWorker: string
  addComment: string
  deliveredLive: string
  requeueWithNote: string
  notePosted: string
  activity: (n: number) => string
  runs: (n: number) => string
  workerLog: string
  workerLogTail: string
  attachments: (n: number) => string
  noAttachments: string
  uploadAttachment: string
  taskActions: string
  copyTaskId: string
  copyTitle: string
  copiedId: (id: string) => string
  copiedTitle: string
  archiveTask: string
  deleteTask: string
  close: string
  working: string
  // board switcher
  board: string
  newBoard: string
  newBoardDots: string
  boardSettings: string
  boardSettingsFor: (name: string) => string
  name: string
  boardNamePlaceholder: string
  slug: (slug: string) => string
  project: string
  noProject: string
  projectHintPre: string
  projectHintCmd: string
  createBoard: string
  // orchestration
  orchestratorProfile: string
  defaultAssignee: string
  defaultParen: string
  autoDecompose: string
  profileDescriptions: string
  profileDescriptionsHint: string
  profileGoodAt: string
  auto: string
}

const en: KanbanMessages = {
  nav: 'Kanban',
  openBoard: 'Kanban: Open board',
  newTaskCommand: 'Kanban: New task',
  countTip: (running, ready) => `Kanban — ${running} running, ${ready} ready`,
  col: {
    triage: { label: 'Triage', help: 'Raw ideas — a specifier fleshes out the spec.' },
    todo: { label: 'Todo', help: 'Waiting on dependencies, or unassigned.' },
    scheduled: { label: 'Scheduled', help: 'Waiting for a scheduled time to arrive.' },
    ready: { label: 'Ready', help: 'Dependencies satisfied — assign a profile and the dispatcher runs it.' },
    running: { label: 'Running', help: 'Claimed by a worker — an agent is on it. Set by the dispatcher.' },
    blocked: { label: 'Blocked', help: 'The worker asked for human input.' },
    review: { label: 'Review', help: 'A review agent is checking the work. Set by the dispatcher.' },
    done: { label: 'Done', help: 'Completed; dependent children become ready.' },
    archived: { label: 'Archived', help: 'Hidden from the default board view.' }
  },
  locked: {
    review: 'Review is entered by the dispatcher when a review agent takes the card.',
    running: 'Running is set by the dispatcher when a worker claims the card.',
    scheduled: 'Scheduled needs a wake-up time — agents set it; it can’t be dragged into.'
  },
  arcRunning: 'An agent is working on this now.',
  arcStale: 'Claimed, but no worker heartbeat for 2+ minutes — the dispatcher will reclaim it.',
  title: 'Kanban',
  orchestrationSettings: 'Orchestration settings',
  newTask: 'New task',
  filterCards: 'Filter cards…',
  noMatch: 'No tasks match the filters',
  noTasks: 'No tasks on this board',
  open: 'Open',
  select: 'Select (⌘-click)',
  deselect: 'Deselect',
  moveTo: label => `Move to ${label}`,
  delete: 'Delete',
  reviewChecking: 'A review agent is checking the completed work.',
  attachedTip: name => `${name} is attached — the dispatcher hands this over on its next tick (≤1m).`,
  orchestratorTip: name => `${name} (the orchestrator) picks this up on the next tick and writes the spec.`,
  autoAssignTip: name => `Auto-assigns to “${name}” (kanban.default_assignee) on the next dispatch tick.`,
  wontRun: "won't run",
  wontRunTip:
    'Ready cards only run once a profile is assigned. Open the card and set an assignee, or configure a default assignee in orchestration settings.',
  noHeartbeat: 'no heartbeat',
  expand: label => `Expand ${label}`,
  collapse: label => `Collapse ${label}`,
  newTaskIn: label => `New task in ${label}`,
  empty: 'Empty',
  unassigned: 'unassigned',
  filters: 'Filters',
  allProfiles: 'All profiles',
  allTenants: 'All tenants',
  showArchived: 'Show archived',
  groupRunning: 'Group Running by profile',
  nSelected: n => `${n} selected`,
  moveToShort: 'Move to',
  assign: 'Assign',
  unassignAction: 'Unassign',
  archive: 'Archive',
  clearSelection: 'Clear selection (Esc)',
  refused: 'refused',
  bulkFailed: (failed, total, err) => `${failed} of ${total} failed — ${err}. Failed cards stay selected.`,
  titlePlaceholderTriage: 'Rough idea — a specifier will flesh it out',
  titlePlaceholder: 'Title',
  descPlaceholder: 'Description (optional)',
  priority: 'Priority',
  workspace: 'Workspace',
  boardDefaultSuffix: ' · board default',
  workspaceOverride: 'Workspace path (optional override)',
  model: 'Model',
  modelInherit: 'Profile default',
  modelClear: 'Clear model override',
  modelHint: 'Runs this task on a specific model and thinking depth. Unset uses the assigned profile’s own.',
  workspaceInherit: 'Inherits the board’s project directory',
  workspaceInheritDir: dir => `Leave empty to inherit ${dir}`,
  workspaceInheritGeneric: 'Leave empty to inherit the board’s project directory.',
  assignee: 'Assignee',
  defaultOption: name => `${name} (default)`,
  parkedOption: "unassigned (parked — won't run)",
  skills: 'Skills (comma-separated)',
  skillsPlaceholder: 'translation, github',
  parent: "Parent (blocks until it's done)",
  noParent: '— no parent —',
  goalMode: "Goal mode (worker loops until a judge agrees it's done)",
  creating: 'Creating…',
  createTask: 'Create task',
  cancel: 'Cancel',
  save: 'Save',
  estimate: 'Estimate',
  estimateEffort: 'Estimate effort',
  estimating: 'Estimating…',
  reEstimate: 'Re-estimate',
  makesModelCall: 'makes a model call',
  estimateTip: 'Rough token + complexity estimate from the auxiliary model — makes a model call.',
  estimateTipLong: 'Runs a quick auxiliary-model call to estimate tokens + complexity. A rough guide, not a bill.',
  roughEstimate: 'Rough estimate',
  tokUnit: 'tok',
  couldNotEstimate: 'Could not estimate',
  complexity: { S: 'Small', M: 'Medium', L: 'Large' },
  introBody:
    'You don’t run the cards — agents do. Put a card in Ready with an assignee and an agent picks it up within a minute. No assignee, no run. Triage: an agent rewrites the idea into a proper task first. Todo: waiting on other cards. Scheduled: waiting on a timer. Running and Review: the agents’ lanes, hands off. Blocked: it’s waiting on you. Results come back on the card.',
  introGotIt: 'Got it',
  evtCreated: (where, assignee) =>
    `created${where ? ` in ${where}` : ''}${assignee ? ` · assigned to ${assignee}` : ''}`,
  evtMovedTo: col => `moved to ${col}`,
  evtParentReopened: parent => `parent ${parent} reopened`,
  evtAssignedTo: assignee => `assigned to ${assignee}`,
  evtUnassigned: 'unassigned',
  evtCommentBy: author => `comment by ${author}`,
  evtClaimedReview: 'claimed by a review agent',
  evtClaimedWorker: 'claimed by a worker',
  evtWorkerStarted: 'worker started',
  evtCompleted: 'completed',
  evtBlocked: 'blocked — needs human input',
  evtUnblocked: col => `unblocked${col ? ` → ${col}` : ' → Ready'}`,
  evtReclaimed: 'reclaimed — returned to the queue',
  evtSpecified: 'spec written by the triage agent',
  evtPromoted: 'dependencies done — promoted to Ready',
  evtScheduled: 'scheduled for later',
  evtArchived: 'archived',
  evtReprioritized: priority => `priority set to ${priority}`,
  someone: 'someone',
  metaPriority: 'Priority',
  metaTenant: 'Tenant',
  metaCreatedBy: 'Created by',
  metaCreated: 'Created',
  metaWorkerPid: 'Worker pid',
  readyUnassignedTitle: 'Ready, but unassigned — this card will never run.',
  readyUnassignedBody:
    'The dispatcher only claims Ready cards that have an assignee. Pick a profile in the Assignee field above (or set a default assignee in the orchestration settings) and it runs within a minute.',
  diagnosticsN: n => `Diagnostics · ${n}`,
  commandCopied: 'Command copied',
  description: 'Description',
  editDescription: 'Edit description',
  cancelEdit: 'Cancel edit',
  noDescription: 'No description yet.',
  result: 'Result',
  latestSummary: 'Latest summary',
  dependencies: 'Dependencies',
  blockedBy: 'Blocked by',
  blocks: 'Blocks',
  comments: n => `Comments · ${n}`,
  commentsHelpRunning:
    'This task is running. Your note is folded into the worker’s current turn within a few seconds — no block/unblock dance. “Requeue with note” instead restarts the task from scratch with your note in context.',
  commentsHelp:
    'Comments are added to the task thread. When a worker picks the task up it reads them as part of its context.',
  send: 'Send',
  comment: 'Comment',
  messageWorker: 'Message the running worker…',
  addComment: 'Add a comment…',
  deliveredLive: 'Delivered to the running worker within a few seconds.',
  requeueWithNote: 'Requeue with note',
  notePosted: 'Note posted — worker requeued',
  activity: n => `Activity · ${n}`,
  runs: n => `Runs · ${n}`,
  workerLog: 'Worker log',
  workerLogTail: 'Worker log · tail',
  attachments: n => `Attachments · ${n}`,
  noAttachments: 'No attachments yet.',
  uploadAttachment: 'Upload attachment',
  taskActions: 'Task actions',
  copyTaskId: 'Copy task id',
  copyTitle: 'Copy title',
  copiedId: id => `Copied ${id}`,
  copiedTitle: 'Copied title',
  archiveTask: 'Archive task',
  deleteTask: 'Delete task',
  close: 'Close',
  working: 'working',
  board: 'Board',
  newBoard: 'New board',
  newBoardDots: 'New board…',
  boardSettings: 'Board settings…',
  boardSettingsFor: name => `Board settings — ${name}`,
  name: 'Name',
  boardNamePlaceholder: 'Board name',
  slug: slug => `slug: ${slug}`,
  project: 'Project',
  noProject: 'No project (scratch sandboxes)',
  projectHintPre:
    'New tasks run in the project’s repo (a worktree per task); each task can still override its workspace at creation. Manage projects with ',
  projectHintCmd: 'hermes project',
  createBoard: 'Create board',
  orchestratorProfile: 'Orchestrator profile',
  defaultAssignee: 'Default assignee',
  defaultParen: '(default)',
  autoDecompose: 'Auto-decompose triage tasks',
  profileDescriptions: 'Profile descriptions',
  profileDescriptionsHint:
    'Descriptions guide the decomposer’s routing. Auto-generate with the auxiliary model, or write your own.',
  profileGoodAt: 'What is this profile good at?',
  auto: 'Auto'
}

const zh: KanbanMessages = {
  nav: '看板',
  openBoard: '看板：打开面板',
  newTaskCommand: '看板：新建任务',
  countTip: (running, ready) => `看板 — 运行中 ${running}、就绪 ${ready}`,
  col: {
    triage: { label: '分诊', help: '原始想法 — 由细化代理整理出规格。' },
    todo: { label: '待办', help: '等待依赖，或未分配。' },
    scheduled: { label: '已排期', help: '等待预定时间到来。' },
    ready: { label: '就绪', help: '依赖已满足 — 分配一个配置档，调度器即会运行它。' },
    running: { label: '运行中', help: '已被工作单元领取 — 有代理在处理。由调度器设置。' },
    blocked: { label: '受阻', help: '工作单元需要人工输入。' },
    review: { label: '审查', help: '审查代理正在检查工作。由调度器设置。' },
    done: { label: '完成', help: '已完成；依赖它的子任务变为就绪。' },
    archived: { label: '已归档', help: '从默认面板视图中隐藏。' }
  },
  locked: {
    review: '审查状态由调度器在审查代理领取卡片时设置。',
    running: '运行中由调度器在工作单元领取卡片时设置。',
    scheduled: '排期需要唤醒时间 — 由代理设置；无法拖入。'
  },
  arcRunning: '有代理正在处理它。',
  arcStale: '已领取，但超过 2 分钟没有工作单元心跳 — 调度器将重新领取。',
  title: '看板',
  orchestrationSettings: '编排设置',
  newTask: '新建任务',
  filterCards: '筛选卡片…',
  noMatch: '没有符合筛选条件的任务',
  noTasks: '此面板暂无任务',
  open: '打开',
  select: '选择（⌘点击）',
  deselect: '取消选择',
  moveTo: label => `移动到 ${label}`,
  delete: '删除',
  reviewChecking: '审查代理正在检查已完成的工作。',
  attachedTip: name => `${name} 已接手 — 调度器将在下一个周期（≤1 分钟）移交。`,
  orchestratorTip: name => `${name}（编排者）将在下一个周期领取并撰写规格。`,
  autoAssignTip: name => `将在下一个调度周期自动分配给“${name}”（kanban.default_assignee）。`,
  wontRun: '不会运行',
  wontRunTip: '就绪卡片只有在分配了配置档后才会运行。打开卡片设置负责人，或在编排设置中配置默认负责人。',
  noHeartbeat: '无心跳',
  expand: label => `展开 ${label}`,
  collapse: label => `折叠 ${label}`,
  newTaskIn: label => `在 ${label} 新建任务`,
  empty: '空',
  unassigned: '未分配',
  filters: '筛选',
  allProfiles: '所有配置档',
  allTenants: '所有租户',
  showArchived: '显示已归档',
  groupRunning: '按配置档分组运行中',
  nSelected: n => `已选择 ${n} 个`,
  moveToShort: '移动到',
  assign: '分配',
  unassignAction: '取消分配',
  archive: '归档',
  clearSelection: '清除选择（Esc）',
  refused: '被拒绝',
  bulkFailed: (failed, total, err) => `${total} 个中有 ${failed} 个失败 — ${err}。失败的卡片仍保持选中。`,
  titlePlaceholderTriage: '大致想法 — 细化代理会补全',
  titlePlaceholder: '标题',
  descPlaceholder: '描述（可选）',
  priority: '优先级',
  workspace: '工作区',
  boardDefaultSuffix: '・面板默认',
  workspaceOverride: '工作区路径（可选覆盖）',
  model: '模型',
  modelInherit: '配置文件默认',
  modelClear: '清除模型覆盖',
  modelHint: '让该任务使用指定的模型与思考深度。未设置时使用所指派配置文件自身的设置。',
  workspaceInherit: '继承面板的项目目录',
  workspaceInheritDir: dir => `留空则继承 ${dir}`,
  workspaceInheritGeneric: '留空则继承面板的项目目录。',
  assignee: '负责人',
  defaultOption: name => `${name}（默认）`,
  parkedOption: '未分配（搁置 — 不会运行）',
  skills: '技能（逗号分隔）',
  skillsPlaceholder: 'translation, github',
  parent: '父任务（完成前会阻塞）',
  noParent: '— 无父任务 —',
  goalMode: '目标模式（工作单元循环直到评判代理认可完成）',
  creating: '创建中…',
  createTask: '创建任务',
  cancel: '取消',
  save: '保存',
  estimate: '估算',
  estimateEffort: '估算工作量',
  estimating: '估算中…',
  reEstimate: '重新估算',
  makesModelCall: '会调用模型',
  estimateTip: '由辅助模型对令牌数和复杂度的粗略估算 — 会调用模型。',
  estimateTipLong: '快速调用辅助模型来估算令牌数和复杂度。仅供参考，并非账单。',
  roughEstimate: '粗略估算',
  tokUnit: 'tok',
  couldNotEstimate: '无法估算',
  complexity: { S: '小', M: '中', L: '大' },
  introBody:
    '卡片不由你运行，而是由代理运行。把带有负责人的卡片放入“就绪”，代理会在一分钟内领取。没有负责人就不会运行。分诊：代理先把想法改写成合适的任务。待办：等待其他卡片。已排期：等待计时器。运行中与审查：这是代理的通道，请勿插手。受阻：正在等你。结果会回到卡片上。',
  introGotIt: '知道了',
  evtCreated: (where, assignee) => `已创建${where ? `（${where}）` : ''}${assignee ? `・分配给 ${assignee}` : ''}`,
  evtMovedTo: col => `移动到 ${col}`,
  evtParentReopened: parent => `父任务 ${parent} 已重新打开`,
  evtAssignedTo: assignee => `分配给 ${assignee}`,
  evtUnassigned: '取消分配',
  evtCommentBy: author => `${author} 的评论`,
  evtClaimedReview: '被审查代理领取',
  evtClaimedWorker: '被工作单元领取',
  evtWorkerStarted: '工作单元已启动',
  evtCompleted: '已完成',
  evtBlocked: '受阻 — 需要人工输入',
  evtUnblocked: col => `已解除阻塞${col ? ` → ${col}` : ' → 就绪'}`,
  evtReclaimed: '已重新领取 — 已放回队列',
  evtSpecified: '分诊代理已撰写规格',
  evtPromoted: '依赖已完成 — 提升为就绪',
  evtScheduled: '已排期稍后运行',
  evtArchived: '已归档',
  evtReprioritized: priority => `优先级设为 ${priority}`,
  someone: '某人',
  metaPriority: '优先级',
  metaTenant: '租户',
  metaCreatedBy: '创建者',
  metaCreated: '创建于',
  metaWorkerPid: '工作单元 PID',
  readyUnassignedTitle: '就绪但未分配 — 这张卡片永远不会运行。',
  readyUnassignedBody:
    '调度器只领取有负责人的就绪卡片。在上面的负责人字段选择一个配置档（或在编排设置中设置默认负责人），它会在一分钟内运行。',
  diagnosticsN: n => `诊断・${n}`,
  commandCopied: '命令已复制',
  description: '描述',
  editDescription: '编辑描述',
  cancelEdit: '取消编辑',
  noDescription: '暂无描述。',
  result: '结果',
  latestSummary: '最新摘要',
  dependencies: '依赖关系',
  blockedBy: '受阻于',
  blocks: '阻塞',
  comments: n => `评论・${n}`,
  commentsHelpRunning:
    '此任务正在运行。你的备注会在几秒内融入工作单元当前的回合 — 无需阻塞/解除操作。选择“附带备注重新入队”则会带着你的备注从头重跑任务。',
  commentsHelp: '评论会添加到任务讨论串。工作单元领取任务时会将其作为上下文的一部分读取。',
  send: '发送',
  comment: '评论',
  messageWorker: '给运行中的工作单元发消息…',
  addComment: '添加评论…',
  deliveredLive: '几秒内送达运行中的工作单元。',
  requeueWithNote: '附带备注重新入队',
  notePosted: '备注已发布 — 工作单元已重新入队',
  activity: n => `活动・${n}`,
  runs: n => `运行・${n}`,
  workerLog: '工作单元日志',
  workerLogTail: '工作单元日志・末尾',
  attachments: n => `附件・${n}`,
  noAttachments: '暂无附件。',
  uploadAttachment: '上传附件',
  taskActions: '任务操作',
  copyTaskId: '复制任务 ID',
  copyTitle: '复制标题',
  copiedId: id => `已复制 ${id}`,
  copiedTitle: '已复制标题',
  archiveTask: '归档任务',
  deleteTask: '删除任务',
  close: '关闭',
  working: '进行中',
  board: '面板',
  newBoard: '新建面板',
  newBoardDots: '新建面板…',
  boardSettings: '面板设置…',
  boardSettingsFor: name => `面板设置 — ${name}`,
  name: '名称',
  boardNamePlaceholder: '面板名称',
  slug: slug => `slug: ${slug}`,
  project: '项目',
  noProject: '无项目（临时沙箱）',
  projectHintPre:
    '新任务将在项目的仓库中运行（每个任务一个 worktree）；每个任务在创建时仍可覆盖其工作区。管理项目请使用 ',
  projectHintCmd: 'hermes project',
  createBoard: '创建面板',
  orchestratorProfile: '编排者配置档',
  defaultAssignee: '默认负责人',
  defaultParen: '（默认）',
  autoDecompose: '自动分解分诊任务',
  profileDescriptions: '配置档说明',
  profileDescriptionsHint: '说明用于引导分解器的路由。可用辅助模型自动生成，或自行填写。',
  profileGoodAt: '这个配置档擅长什么？',
  auto: '自动'
}

export const KANBAN_LOCALES: PluginLocaleBundles = { en, zh }

// Bind the message SHAPE to a plugin translator: string leaves resolve now,
// function leaves forward their args through t(path, …). One tiny generic
// instead of a hand-written accessor per key.
type Bound<T> = {
  [K in keyof T]: T[K] extends (...args: infer A) => string
    ? (...args: A) => string
    : T[K] extends object
      ? Bound<T[K]>
      : string
}

function bind<T extends object>(t: PluginTranslate, template: T, prefix = ''): Bound<T> {
  const out = {} as Record<string, unknown>

  for (const [key, value] of Object.entries(template)) {
    const path = prefix ? `${prefix}.${key}` : key
    out[key] =
      typeof value === 'function'
        ? (...args: unknown[]) => t(path, ...args)
        : value && typeof value === 'object'
          ? bind(t, value as object, path)
          : t(path)
  }

  return out as Bound<T>
}

export type KanbanText = Bound<KanbanMessages>

/** The kanban strings for the active locale — one hook every component reads. */
export function useKanban(): KanbanText {
  const t = usePluginI18n('kanban')

  return useMemo(() => bind(t, en), [t])
}

// Column labels/help live in i18n; unknown backend statuses fall back to the id.
export const columnLabel = (k: KanbanText, name: string) => k.col[name as keyof KanbanText['col']]?.label ?? name
export const columnHelp = (k: KanbanText, name: string) => k.col[name as keyof KanbanText['col']]?.help ?? ''
export const lockedReason = (k: KanbanText, name: string) => k.locked[name as keyof KanbanText['locked']] ?? ''
