-- 技能隔离事件与样本（阶段 7 · S7-C，上行）
--
-- 执行方式：
--   mysql -u root -p audit_gateway < scripts/migrations/2026-08-26-skill-security-events.sql
--
-- 产品前提：技能来源**不封禁**。自有 skill hub 是给各部门下技能指标用的，
-- 把员工的工作经验转成公司资产，所以下载要放开。安全靠安装时与定期扫描兜底：
-- 命中就隔离、样本传到这里供人工审查；员工可以自助恢复，但恢复要留一行。
--
-- 所以 skill_security_events 不是「拦截记录」，是**越过记录**。
--
-- ⚠️ files_json 必须是 MEDIUMTEXT，不能是 TEXT
--
--   TEXT 只有 64KB，客户端的样本上限是 512KB。塞进 TEXT 的后果是 MySQL
--   非严格模式下**静默截断** —— 库里那一行看上去好好的，审查的人读到的却是
--   半截代码，而恰恰是被截掉的后半段最可能是藏东西的地方。
--   接收接口另有一道 2MB 的体积检查，两道都留着。
--
-- ⚠️ 身份列不来自客户端载荷
--
--   user_uid / dept_id 由网关从 API Key 解出来。载荷里带了也忽略：客户端跑在
--   员工自己的机器上，它说自己是谁没有意义，而这张表的用途恰恰是「谁越过了
--   扫描结论」。device 是客户端自报的，仅供人工定位机器，不参与任何判定。
--
-- ⚠️ occurred_at / reported_at 是 VARCHAR 不是 DATETIME
--
--   客户端 UTC 字符串原样存。与 client_releases.released_at 同一个理由：写它的
--   机器时区不受我们控制。排序和报表一律用 created_at（服务器本地时间，
--   全库统一口径，见 model/database.now）。
--
-- 样本按内容指纹去重（sample_hash = 路径+各文件 sha256 的哈希，服务端算）。
--   不用客户端上报的 bundle_hash 当去重键：那个字段可能为空，空值当键会把
--   互不相干的样本合成一条。

CREATE TABLE IF NOT EXISTS `skill_security_samples` (
  `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增ID',
  `sample_hash` VARCHAR(64) NOT NULL COMMENT '样本内容指纹，去重键（服务端算）',
  `bundle_hash` VARCHAR(64) NULL COMMENT '客户端算的技能包哈希，用于与事件互相串联',

  `file_count` INT NOT NULL DEFAULT 0 COMMENT '技能包里的文件总数',
  `included_count` INT NOT NULL DEFAULT 0 COMMENT '带内容上传的文件数',
  `included_bytes` BIGINT NOT NULL DEFAULT 0 COMMENT '带内容上传的字节数',
  `truncated` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否有文件因超限只留了元数据',
  `truncated_reason` VARCHAR(32) NULL COMMENT 'file_count/total_bytes',
  `files_json` MEDIUMTEXT NOT NULL COMMENT '文件清单与内容原文 JSON（超限的只有元数据）',

  `seen_count` INT NOT NULL DEFAULT 1 COMMENT '这份样本被上报过多少次',
  `first_seen_at` DATETIME NOT NULL COMMENT '首次上报时间',
  `last_seen_at` DATETIME NOT NULL COMMENT '最近一次上报时间',

  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_skill_sample_hash` (`sample_hash`),
  KEY `ix_skill_samples_bundle` (`bundle_hash`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='被隔离技能的样本本体，按内容去重';


CREATE TABLE IF NOT EXISTS `skill_security_events` (
  `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增ID',
  `event_id` VARCHAR(64) NOT NULL COMMENT '客户端生成的事件ID，幂等键',
  `event` VARCHAR(32) NOT NULL COMMENT 'skill_quarantined / skill_quarantine_restored',

  `user_uid` VARCHAR(36) NOT NULL COMMENT '上报人uid（网关从 API Key 解出来的）',
  `dept_id` INT NULL COMMENT '上报时所属部门（快照，不随调岗变）',
  `dept_code` VARCHAR(64) NULL COMMENT '上报时部门代码（快照）',
  `device` VARCHAR(128) NULL COMMENT '客户端自报的机器标签，仅供人工定位',

  `skill_name` VARCHAR(255) NOT NULL COMMENT '技能名',
  `entry_id` VARCHAR(64) NULL COMMENT '客户端隔离条目ID，同机上串联隔离与恢复',
  `verdict` VARCHAR(16) NULL COMMENT '扫描结论 dangerous/caution/safe',
  `reason` VARCHAR(512) NULL COMMENT '隔离原因（扫描器给的摘要）',
  `source` VARCHAR(64) NULL COMMENT '技能来源',
  `origin_relpath` VARCHAR(512) NULL COMMENT '技能在活动树里的相对路径',

  `scanner_version` VARCHAR(32) NULL COMMENT '扫描器版本，结论随它变',
  `bundle_hash` VARCHAR(64) NULL COMMENT '技能包哈希，串联隔离与恢复两条事件',
  `finding_count` INT NOT NULL DEFAULT 0 COMMENT '命中项数量',
  `findings_json` TEXT NULL COMMENT '命中项明细 JSON',

  `sample_id` BIGINT NULL COMMENT '样本ID；恢复事件为空（样本隔离时已传过）',

  `occurred_at` VARCHAR(24) NULL COMMENT '客户端本地记录的发生时间（UTC ISO 原文）',
  `reported_at` VARCHAR(24) NULL COMMENT '客户端入队时间（UTC ISO 原文）',
  `schema_version` INT NOT NULL DEFAULT 1 COMMENT '客户端载荷版本',
  `created_at` DATETIME NOT NULL COMMENT '网关收到的时间',

  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_skill_event_id` (`event_id`),
  KEY `ix_skill_events_user` (`user_uid`, `created_at`),
  KEY `ix_skill_events_dept` (`dept_id`, `created_at`),
  KEY `ix_skill_events_kind` (`event`, `created_at`),
  KEY `ix_skill_events_bundle` (`bundle_hash`),
  KEY `ix_skill_events_sample` (`sample_id`),
  KEY `ix_skill_events_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='技能隔离与自助恢复的留痕：越过扫描结论的动作在这里留一行';
