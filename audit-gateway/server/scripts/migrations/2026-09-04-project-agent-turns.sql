-- 群聊 agent 运行时：触发范围 + 执行租约（阶段 27-A）
--
-- 两件事：
--
-- 1. `project_members.respond_to` —— 谁可以指挥这个 agent（P0-D）。存量行落
--    `project_members`，与加列前的行为逐字一致：加列前谁都能 @ 它，加列后
--    默认仍是谁都能 @ 它，但那只换来一个**无能力的群聊回复**。
--
-- 2. `project_agent_turns` —— 执行租约（P0-C / P0-E）。每个 agent 跑在它主人
--    自己的机器上，所以「一个项目同时只有一个 in-flight turn」是跨机器约束，
--    只能压在数据库的唯一索引上。它同时是 agent 写回时的身份来源 —— 没有它，
--    「agent 在群里发言」只能长成「客户端说自己是 agent 就当 agent」。
--
-- ⚠️ 连续 agent 对话的跳数**不建列**：它从 project_messages 尾部连续的 agent
--    发言数推得出来，另存一份计数只会与消息表漂移，而漂移的表现是熔断在错误的
--    时候触发或不触发。

ALTER TABLE `project_members`
  ADD COLUMN `respond_to` VARCHAR(16) NOT NULL DEFAULT 'project_members'
    COMMENT 'owner_only / allowlist / project_members / nobody；仅 agent 成员有意义',
  ADD COLUMN `respond_allowlist` TEXT NULL
    COMMENT 'respond_to=allowlist 时的成员 uid 列表（JSON 数组）';

-- `inflight_key` 在跑时 = project_id，进终态时置 NULL。
-- MySQL 的唯一索引允许多个 NULL，这正好表达「已结束的 turn 想有多少有多少，
-- 在跑的只能有一个」—— 部分唯一索引 MySQL 没有，这是等价写法。
CREATE TABLE IF NOT EXISTS `project_agent_turns` (
  `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增ID',
  `project_id` BIGINT NOT NULL COMMENT '所属项目',
  `inflight_key` BIGINT NULL COMMENT '在跑时=project_id，结束置空',

  `member_id` BIGINT NOT NULL COMMENT '哪个 agent 成员（project_members.id）',
  `agent_label` VARCHAR(64) NOT NULL DEFAULT '' COMMENT 'agent 显示名，写回用',
  `owner_uid` VARCHAR(36) NOT NULL COMMENT 'agent 主人的 uid，也是记账对象',

  `trigger_reason` VARCHAR(16) NOT NULL
    COMMENT 'mention / task_assigned / task_event / owner_request；P0 只有这四种',
  `trigger_seq` BIGINT NOT NULL COMMENT '触发事件在项目内的 seq',
  `trigger_message_id` BIGINT NULL COMMENT '触发的群聊消息',
  `trigger_task_id` BIGINT NULL COMMENT '触发的任务',
  `requester_uid` VARCHAR(36) NOT NULL COMMENT '谁触发的（权限判定的依据）',
  `trigger_request_id` VARCHAR(64) NOT NULL COMMENT '串 usage/拒绝日志的 request_id',
  `batch_json` TEXT NULL COMMENT '同批其它触发事件的快照（JSON）',

  `hop_index` INT NOT NULL DEFAULT 0 COMMENT '这一跳是连续 agent 对话的第几跳',

  `status` VARCHAR(16) NOT NULL DEFAULT 'queued'
    COMMENT 'queued / running / completed / failed / cancelled',
  `status_seq` INT NOT NULL DEFAULT 0 COMMENT '状态水位，只前进',
  `attempt` INT NOT NULL DEFAULT 1 COMMENT '第几次尝试，含重试',
  `error_code` VARCHAR(64) NULL COMMENT '失败原因，进死信时要看得见',

  `lease_expires_at` DATETIME NULL COMMENT '租约到期时间；续不上即视为死亡',
  `started_at` DATETIME NULL COMMENT '进入 running 的时间',
  `finished_at` DATETIME NULL COMMENT '进入终态的时间',
  `reply_message_id` BIGINT NULL COMMENT '这次 turn 写回的那条消息',

  `created_at` DATETIME NOT NULL COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL COMMENT '更新时间',

  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_project_inflight` (`inflight_key`),
  KEY `ix_project_turns_project` (`project_id`, `status`),
  KEY `ix_project_turns_owner` (`owner_uid`, `status`),
  KEY `ix_project_turns_trigger` (`trigger_request_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='群聊 agent 执行租约：单项目单 in-flight，兼作写回身份来源';
