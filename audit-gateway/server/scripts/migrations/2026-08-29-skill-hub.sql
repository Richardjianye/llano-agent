-- 自有 skill hub（阶段 11 · S11-A）
--
-- 执行方式：
--   mysql -u root -p audit_gateway < scripts/migrations/2026-08-29-skill-hub.sql
--
-- 这是什么：公司内部的技能市场。产品前提（2026-08-26 负责人定）是给各部门下
-- 技能指标 —— 每月做多少 skill、总结多少工作经验，把员工的工作经验转成公司资产。
-- 所以这两张表的重点不只是「有哪些技能」，还有**是谁发的、算谁的**。
--
-- 与第三方来源的关系：hub 是**增量不是替代**。技能来源不封禁的决定没有变，
-- 员工照样能从 github / clawhub 装东西，hub 多提供一条「公司自己的」。
--
-- 流程是**先发后审**：发布即全公司可见，管理员事后可下架。跟技能安全那一套
-- 同一个取向 —— 不拦人、留痕、事后处理。审核队列会卡住技能指标，而指标正是
-- 这东西存在的理由。
--
-- ⚠️ 网关侧**不扫描**发布上来的技能包（第一期有意识的取舍）
--
--   tools/skills_guard.py 是客户端 Python 模块，网关是独立服务不共享代码，
--   移植是笔真投资。安全仍由客户端已有的三道兜：安装时扫描、6 小时定期重扫、
--   命中即隔离并上传样本。hub 相对第三方来源的关键差异是**发布者实名** ——
--   publisher_uid 由网关从 API Key 解出来，出事能追到人。
--
--   代价要说清楚：一个坏技能会先分发出去，再由各机器分别拦下，而不是在发布
--   那一刻就被挡住。哪天觉得这个代价不能接受，要做的是把扫描器做成两边共享
--   的包，而不是在网关里另写一套规则（两套规则集会各自漂移，比没有更糟）。
--
-- ⚠️ 身份列不来自客户端载荷
--
--   owner_uid / publisher_uid / dept_* 一律由网关从 API Key 解出来。载荷里带了
--   也忽略 —— 客户端跑在员工自己的机器上，它说自己是谁没有意义，而这两张表的
--   用途之一恰恰是部门指标统计。
--
-- ⚠️ files_json 必须是 MEDIUMTEXT，不能是 TEXT
--
--   TEXT 只有 64KB。塞进去的后果是 MySQL 非严格模式下**静默截断**，而这张表存的
--   是会被装到几十台机器上执行的东西：分发出去的是残包，客户端解析 JSON 直接
--   失败，员工看到的是「装不上」，离真正的原因隔着好几层。
--   接收接口另有一道体积检查，两道都留着。

CREATE TABLE IF NOT EXISTS `skill_hub_skills` (
  `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增ID',
  `slug` VARCHAR(64) NOT NULL COMMENT '安装标识，全公司唯一（也是磁盘目录名）',
  `name` VARCHAR(255) NOT NULL COMMENT '显示名',
  `description` VARCHAR(1024) NOT NULL DEFAULT '' COMMENT '一句话说明',

  `latest_version_id` BIGINT NULL COMMENT '当前该装的版本ID',
  `latest_version` INT NOT NULL DEFAULT 0 COMMENT '当前版本号，从1递增',

  `owner_uid` VARCHAR(36) NOT NULL COMMENT '首发人uid（网关从 API Key 解出来的）',
  `owner_username` VARCHAR(64) NULL COMMENT '首发人用户名快照',
  `owner_dept_id` INT NULL COMMENT '首发时所属部门（快照，不随调岗变）',
  `owner_dept_code` VARCHAR(64) NULL COMMENT '首发时部门代码（快照）',

  `status` VARCHAR(16) NOT NULL DEFAULT 'listed' COMMENT 'listed / delisted',
  `delisted_reason` VARCHAR(512) NULL COMMENT '下架原因，员工搜不到时后台能答上来',
  `delisted_by_uid` VARCHAR(36) NULL COMMENT '下架操作人uid',
  `delisted_at` DATETIME NULL COMMENT '下架时间',

  `install_count` INT NOT NULL DEFAULT 0 COMMENT '被取包次数，粗略的热度',

  `created_at` DATETIME NOT NULL COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL COMMENT '更新时间',

  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_skill_hub_slug` (`slug`),
  KEY `ix_skill_hub_owner` (`owner_uid`, `created_at`),
  KEY `ix_skill_hub_dept` (`owner_dept_id`, `created_at`),
  KEY `ix_skill_hub_status` (`status`, `updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='自有 skill hub：技能身份与归属';


CREATE TABLE IF NOT EXISTS `skill_hub_versions` (
  `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增ID',
  `skill_id` BIGINT NOT NULL COMMENT '所属技能ID',
  `version` INT NOT NULL COMMENT '版本号，服务端从1递增（不是 semver，见下）',

  `bundle_hash` VARCHAR(64) NOT NULL COMMENT '内容指纹（服务端按 路径+各文件sha256 算）',
  `files_json` MEDIUMTEXT NOT NULL COMMENT '文件清单与内容，逐文件带 utf-8/base64 编码信封',
  `file_count` INT NOT NULL DEFAULT 0 COMMENT '文件数',
  `total_bytes` BIGINT NOT NULL DEFAULT 0 COMMENT '解码后的原始字节数',

  `changelog` VARCHAR(1024) NULL COMMENT '这一版改了什么，发布者填',

  `publisher_uid` VARCHAR(36) NOT NULL COMMENT '发布人uid（网关解出来的）',
  `publisher_username` VARCHAR(64) NULL COMMENT '发布人用户名快照',
  `publisher_dept_id` INT NULL COMMENT '发布时所属部门（快照）',
  `publisher_dept_code` VARCHAR(64) NULL COMMENT '发布时部门代码（快照）',

  `created_at` DATETIME NOT NULL COMMENT '发布时间',

  PRIMARY KEY (`id`),
  -- 同一技能的版本号唯一：并发发布时靠它把「两个人同时算出 v3」挡成一个失败，
  -- 而不是库里出现两行 v3。
  UNIQUE KEY `uq_skill_hub_version` (`skill_id`, `version`),
  -- 同一技能同样内容不新增版本：否则员工每按一次发布就多一版，
  -- 版本号会变成按钮点击次数。
  UNIQUE KEY `uq_skill_hub_bundle` (`skill_id`, `bundle_hash`),
  KEY `ix_skill_hub_ver_skill` (`skill_id`),
  KEY `ix_skill_hub_ver_publisher` (`publisher_uid`, `created_at`),
  KEY `ix_skill_hub_ver_dept` (`publisher_dept_id`, `created_at`),
  KEY `ix_skill_hub_ver_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='自有 skill hub：每一版的内容本体';
