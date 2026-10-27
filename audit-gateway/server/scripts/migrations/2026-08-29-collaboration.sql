-- 协作（项目群聊）—— 阶段 13
--
-- 执行方式：
--   mysql -u root -p audit_gateway < scripts/migrations/2026-08-29-collaboration.sql
--
-- ⚠️ 这个文件里有 ALTER，不只是 CREATE。`create_all` 只建缺失的表，**不给已有表
--   加列**，所以 usage_records 那两列必须在这里加。漏跑的表现是服务照常启动、
--   直到第一次转发写用量才炸 Unknown column，而用户看到的是「对话失败」。
--   （启动自检 warn_schema_drift 会在 dev 告警、prod 拒绝启动。）
--
-- 设计决议见 TODO「协作（群聊）功能设计决议」整章。这里只重复会直接影响建模、
-- 且**事后补不了**的三条：
--
-- P0-3 审计模型要从一对一扩到多方。只加一个 group_id 是不够的 ——「谁说的」
--      「agent 的回复归属谁」「哪条消息触发了哪次出网调用」三个问题都答不上来。
-- P0-4 项目记忆与个人 agent 记忆从一开始就分开存。这是离职处理的前置条件：
--      混在一起的话，离职当天只剩「连人带项目知识一起带走」或「把个人数据强留在
--      公司」两个都错的选项。
-- P0-5 不提前建多租户，但新表一律带项目作用域字段，且不允许出现
--      「全局唯一 = 公司唯一」的隐含假设。
--
-- ⚠️ project_messages 同时存脱敏后与原文（P0-2）
--
--   现有脱敏只覆盖「发往模型上游」这一个方向。群聊里发给其他部门 / 其他公司的人
--   同样是内容外流，必须走**同一份关键词表**，否则关键词防线在协作场景下直接
--   失效，而且失效得很安静。content_original 与 conversation_logs 的原文同级敏感，
--   受同一套留存期管辖。

CREATE TABLE IF NOT EXISTS `projects` (
  `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增ID',
  `code` VARCHAR(64) NOT NULL COMMENT '项目标识，创建后不可改（改了等于换项目）',
  `name` VARCHAR(255) NOT NULL COMMENT '显示名',
  `description` VARCHAR(1024) NOT NULL DEFAULT '' COMMENT '一句话说明',

  `owner_uid` VARCHAR(36) NOT NULL COMMENT '当前负责人uid（换人=改这里，无继承语义，见决策#46）',
  `created_by_uid` VARCHAR(36) NOT NULL COMMENT '创建人uid（不随换人变）',
  `created_dept_id` INT NULL COMMENT '创建时所属部门（快照）',
  `created_dept_code` VARCHAR(64) NULL COMMENT '创建时部门代码（快照）',

  `status` VARCHAR(16) NOT NULL DEFAULT 'active' COMMENT 'active / archived',
  `archived_at` DATETIME NULL COMMENT '归档时间',
  `archived_reason` VARCHAR(512) NULL COMMENT '归档原因',

  `git_repo_url` VARCHAR(512) NULL COMMENT '自建 Gitea 仓库地址；对接前为空（决策#47）',

  -- 消息序号的发号器。靠 max(seq)+1 查的话，并发发言必然撞。
  `message_seq` BIGINT NOT NULL DEFAULT 0 COMMENT '项目内消息序号水位',

  `created_at` DATETIME NOT NULL COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL COMMENT '更新时间',

  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_project_code` (`code`),
  KEY `ix_projects_owner` (`owner_uid`),
  KEY `ix_projects_status` (`status`, `updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='协作项目';


CREATE TABLE IF NOT EXISTS `project_members` (
  `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增ID',
  `project_id` BIGINT NOT NULL COMMENT '所属项目',

  -- agent 是**一等成员**，不是人的一个布尔字段。记成布尔的话「这条消息是谁说的」
  -- 就答不清楚，而那正是 P0-3 要求的。
  `member_kind` VARCHAR(8) NOT NULL COMMENT 'user / agent',
  `user_uid` VARCHAR(36) NOT NULL COMMENT '人成员=本人uid；agent 成员=**主人**uid（归属与记账）',
  `username` VARCHAR(64) NULL COMMENT '用户名快照',
  `agent_label` VARCHAR(64) NOT NULL DEFAULT '' COMMENT 'agent 显示名；人成员为空串',

  `dept_id` INT NULL COMMENT '加入时所属部门（快照）',
  `dept_code` VARCHAR(64) NULL COMMENT '加入时部门代码（快照）',

  `role` VARCHAR(16) NOT NULL DEFAULT 'member' COMMENT 'owner / member / viewer',
  -- 退出**不删行**：历史发言还挂在这个成员上，删了那些发言就没有主语了。
  -- 而且退群只解决可见性——已流入群聊历史的内容收不回来，退出不能当补救手段用。
  `status` VARCHAR(16) NOT NULL DEFAULT 'active' COMMENT 'active / left',
  `joined_at` DATETIME NOT NULL COMMENT '加入时间',
  `left_at` DATETIME NULL COMMENT '退出/被移除时间',
  `left_reason` VARCHAR(255) NULL COMMENT '退出原因，供事后追',

  `created_at` DATETIME NOT NULL COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL COMMENT '更新时间',

  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_project_member` (`project_id`, `member_kind`, `user_uid`, `agent_label`),
  KEY `ix_project_members_uid` (`user_uid`, `status`),
  KEY `ix_project_members_project` (`project_id`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='项目成员：人或某个人的 agent';


CREATE TABLE IF NOT EXISTS `project_messages` (
  `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增ID',
  `project_id` BIGINT NOT NULL COMMENT '所属项目',
  -- 项目内单调序号。用全局 id 当读游标的话，项目 A 的游标会因为项目 B 的写入
  -- 而跳号，看起来像丢了消息。
  `seq` BIGINT NOT NULL COMMENT '项目内序号，读游标用',

  `sender_kind` VARCHAR(8) NOT NULL COMMENT 'user / agent / system',
  `sender_uid` VARCHAR(36) NOT NULL COMMENT '人=本人；agent=**主人**uid（归属与记账）',
  `sender_username` VARCHAR(64) NULL COMMENT '用户名快照',
  `sender_agent_label` VARCHAR(64) NULL COMMENT 'agent 显示名；人发言为空',
  `sender_dept_id` INT NULL COMMENT '发言时所属部门（快照）',
  `sender_dept_code` VARCHAR(64) NULL COMMENT '发言时部门代码（快照）',

  `reply_to_id` BIGINT NULL COMMENT '回复的哪一条',
  `mentions_json` TEXT NULL COMMENT '被 @ 的成员，决定强制触发',

  `content` MEDIUMTEXT NOT NULL COMMENT '脱敏后正文（投递给成员的就是这份）',
  `content_original` MEDIUMTEXT NULL COMMENT '脱敏前原文，仅审计可见，与会话原文同级敏感',
  `redaction_hits` INT NOT NULL DEFAULT 0 COMMENT '命中关键词数，0 表示两份相同',

  `visibility` VARCHAR(16) NOT NULL DEFAULT 'project' COMMENT 'project；留待将来收窄',
  `trigger_request_id` VARCHAR(64) NULL COMMENT '这条消息触发的出网调用，串 usage/mcp 日志',

  `created_at` DATETIME NOT NULL COMMENT '服务器本地时间，全库统一口径',

  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_project_message_seq` (`project_id`, `seq`),
  KEY `ix_project_messages_project` (`project_id`, `seq`),
  KEY `ix_project_messages_sender` (`sender_uid`, `created_at`),
  KEY `ix_project_messages_trigger` (`trigger_request_id`),
  KEY `ix_project_messages_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='项目群聊消息：P0-3 的多方审计模型';


CREATE TABLE IF NOT EXISTS `project_tasks` (
  `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增ID',
  `project_id` BIGINT NOT NULL COMMENT '所属项目',
  `seq` INT NOT NULL COMMENT '项目内任务号，给人念的',

  `title` VARCHAR(255) NOT NULL COMMENT '标题',
  -- ⚠️ 正文是**不可信输入**。它会被原样呈给认领人看（那正是防线所在），
  --    但绝不能在任何地方被当作指令直接执行。
  `body` TEXT NULL COMMENT '任务描述（不可信输入，原样保留）',

  -- ---- P0-6 的核心：认领会把污染从群聊带回私聊 ----
  --
  -- 决策 #45 把能力挡在群聊之外（阶段 12 已落地），但攻击路径可以绕道：
  -- 群里发一个夹带注入的任务描述 → agent 认领 → 回到私聊上下文执行 →
  -- 此时它持有主人的全部 MCP 凭据。执行不发生在群聊，群聊禁用能力就完全落空。
  `source_kind` VARCHAR(16) NOT NULL DEFAULT 'chat'
    COMMENT 'chat=群聊消息 / manual=成员手写；决定 untrusted 的默认值',
  `source_message_id` BIGINT NULL COMMENT '来自哪条群聊消息',
  -- 默认 1：群里来的一律不可信。反过来（默认可信、遇到群聊才标脏）漏一处
  -- 就等于这条防线不存在。
  `untrusted` TINYINT(1) NOT NULL DEFAULT 1
    COMMENT '不可信标记，跟着任务描述进入私聊上下文，不因为「这是主人的会话」而清除',

  `created_by_uid` VARCHAR(36) NOT NULL COMMENT '创建人uid（网关解出来的）',
  `created_by_username` VARCHAR(64) NULL COMMENT '创建人用户名快照',

  -- ---- 认领与开工是**两步** ----
  -- 「认领 ≠ 自动开工，认领后需主人显式确认才进入执行，确认时把任务原文原样
  --   呈给他看。人在环这一下比任何自动检测都可靠，成本也极低。」
  `status` VARCHAR(16) NOT NULL DEFAULT 'open'
    COMMENT 'open=未认领 / claimed=已认领待确认 / active=已开工 / done / cancelled',
  `claimed_by_uid` VARCHAR(36) NULL COMMENT '认领人uid；停用账号时按它退回未认领池',
  `claimed_by_username` VARCHAR(64) NULL COMMENT '认领人用户名快照',
  `claimed_agent_label` VARCHAR(64) NULL COMMENT '打算用哪个 agent 干',
  `claimed_at` DATETIME NULL COMMENT '认领时间',
  `confirmed_at` DATETIME NULL COMMENT '主人确认开工的时间；没有它就只是 claimed',
  `done_at` DATETIME NULL COMMENT '完成时间',
  `returned_count` INT NOT NULL DEFAULT 0 COMMENT '被退回未认领池几次',

  `created_at` DATETIME NOT NULL COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL COMMENT '更新时间',

  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_project_task_seq` (`project_id`, `seq`),
  KEY `ix_project_tasks_project` (`project_id`, `status`),
  KEY `ix_project_tasks_claimer` (`claimed_by_uid`, `status`),
  KEY `ix_project_tasks_source` (`source_message_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='项目任务：认领≠开工，不可信标记跟着描述走（P0-6）';


-- ==================== 用量口径（决策 #48）====================
--
-- 决策 #48 当时写的是「现在就要动手，不等 v2」，但一直没落。补的理由没变：
-- 群聊里 agent 的路由打分是**真实 token 消耗但没有任何可见输出**。按现行口径它
-- 要么漏记（违反既有记账纪律），要么混进「请求数」与「上游成功率」——而后者是用来
-- 判断合作厂商稳不稳的指标，被一堆没人看见的打分调用稀释就废了。
--
-- 默认值让所有历史行与既有写入路径自动落到 chat / private，不需要回填。

ALTER TABLE `usage_records`
  ADD COLUMN `call_kind` VARCHAR(16) NOT NULL DEFAULT 'chat'
    COMMENT 'chat=对话（计入请求数与成功率）/ gate=群聊路由打分（只计费）/ agent=agent 自主发言'
    AFTER `stream`,
  ADD COLUMN `trigger_source` VARCHAR(16) NOT NULL DEFAULT 'private'
    COMMENT 'private=私聊 / project=项目群聊'
    AFTER `call_kind`,
  ADD KEY `ix_usage_call_kind` (`call_kind`);
