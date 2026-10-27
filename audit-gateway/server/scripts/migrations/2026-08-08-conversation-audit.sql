-- 会话审计：附件表 + 查阅日志表 + conversation_logs 加列
--
-- ⚠️ **按文件名顺序跑。** 前置：2026-08-07-usage-reporting.sql
--    后续：2026-08-08-timezone-and-pricing.sql → 2026-08-08-billing-modes-and-abort.sql
--
-- 为什么需要这个文件：建表走 SQLAlchemy 的 create_all，它只「建缺失的表」，
-- **不会给已存在的表加列**。新增的两张表 create_all 会自动建出来，
-- 但 `conversation_logs.attachment_count` 这一列必须手工加，
-- 否则启动不报错、第一次写审计时才炸在 Unknown column 上（已经踩过一次了）。
--
-- 执行：
--   mysql -u root -p audit_gateway < scripts/migrations/2026-08-08-conversation-audit.sql
--
-- 幂等性：MySQL 8.0 不支持 ADD COLUMN IF NOT EXISTS，重复执行会报
-- "Duplicate column name" / "Table already exists"。那两个报错都是安全的。
-- （治本方案是引入 Alembic，已记在 README 的「已知待办」里。）

-- ---------- conversation_logs：附件数量冗余列 ----------
-- 列表页要给带附件的行打回形针标记。为此每行去 count 一次附件表不值当，
-- 写入时顺手记下来即可。
ALTER TABLE `conversation_logs`
  ADD COLUMN `attachment_count` INT NOT NULL DEFAULT 0 COMMENT '本次请求携带的附件数（冗余，免 join）';

-- ---------- 会话附件记录 ----------
-- 多模态消息里的图片常以 base64 data URL 内联，一张截图几 MB，
-- 原样塞进 request_messages 会撑爆那一列、还会触发截断把后面的文字一起吃掉。
-- 所以抽出来单独存，原文里只留 [[attachment:短码]] 占位符。
CREATE TABLE IF NOT EXISTS `conversation_attachments` (
  `id`              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '自增ID',
  `request_id`      VARCHAR(64)  NOT NULL COMMENT '请求ID，串联同一次调用',
  `conversation_id` BIGINT       NULL COMMENT '所属 conversation_logs.id',
  `user_uid`        VARCHAR(36)  NOT NULL COMMENT '上传者uid',
  `dept_id`         INT          NULL COMMENT '上传时所属部门（快照）',
  `marker`          VARCHAR(64)  NOT NULL COMMENT '原文占位符短码',
  `kind`            VARCHAR(16)  NOT NULL DEFAULT 'file' COMMENT 'image/file/audio',
  `mime_type`       VARCHAR(128) NULL COMMENT 'MIME 类型',
  `filename`        VARCHAR(255) NULL COMMENT '原始文件名',
  `size_bytes`      BIGINT       NOT NULL DEFAULT 0 COMMENT '字节数',
  `sha256`          VARCHAR(64)  NULL COMMENT '内容哈希，用于去重与取证',
  -- 远程 URL 形态的附件只记 URL 不抓取：网关主动去下载用户给的任意地址
  -- 是个 SSRF 面，不值得为审计完整性开这个口子
  `origin_url`      VARCHAR(1024) NULL COMMENT '远程附件的原始URL（不抓取）',
  `storage_path`    VARCHAR(512) NULL COMMENT '落盘相对路径，未落盘为空',
  `stored`          TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '文件本体是否已落盘',
  `store_error`     VARCHAR(255) NULL COMMENT '落盘失败原因',
  `created_at`      DATETIME     NOT NULL COMMENT '发生时间',
  PRIMARY KEY (`id`),
  KEY `ix_conversation_attachments_request_id` (`request_id`),
  KEY `ix_conversation_attachments_conversation_id` (`conversation_id`),
  KEY `ix_conversation_attachments_user_uid` (`user_uid`),
  KEY `ix_conversation_attachments_sha256` (`sha256`),
  KEY `ix_conversation_attachments_created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='会话附件记录';

-- ---------- 会话查阅日志 ----------
-- 「看的人也被看」。与 credential_access_logs 分开：那张管中台凭证解密，
-- 这张管聊天内容查阅，留存期限和审批流程都不一样。
CREATE TABLE IF NOT EXISTS `audit_access_logs` (
  `id`                     BIGINT       NOT NULL AUTO_INCREMENT COMMENT '自增ID',
  `actor_uid`              VARCHAR(36)  NOT NULL COMMENT '查阅人uid',
  `actor_role`             VARCHAR(16)  NULL COMMENT '查阅时角色（快照）',
  `actor_dept_id`          INT          NULL COMMENT '查阅时部门（快照）',
  `action`                 VARCHAR(16)  NOT NULL COMMENT 'list/detail/attachment/export',
  `target_conversation_id` BIGINT       NULL COMMENT '被查阅的会话ID，列表/导出为空',
  `target_attachment_id`   BIGINT       NULL COMMENT '被查阅的附件ID',
  `target_user_uid`        VARCHAR(36)  NULL COMMENT '被查阅内容的归属人uid',
  -- 查询条件原样存：日后要回答「他是不是在专门盯着某个人」，
  -- 靠的就是这一列里反复出现的 user_uid
  `query_json`             TEXT         NULL COMMENT '查询条件原文 JSON',
  `result_count`           INT          NOT NULL DEFAULT 0 COMMENT '本次返回的条数',
  `request_id`             VARCHAR(64)  NULL COMMENT '请求ID',
  `client_ip`              VARCHAR(64)  NULL COMMENT '来源IP',
  `user_agent`             VARCHAR(255) NULL COMMENT 'User-Agent，区分后台/脚本',
  `created_at`             DATETIME     NOT NULL COMMENT '发生时间',
  PRIMARY KEY (`id`),
  KEY `ix_audit_access_logs_actor_uid` (`actor_uid`),
  KEY `ix_audit_access_logs_action` (`action`),
  KEY `ix_audit_access_logs_target_conversation_id` (`target_conversation_id`),
  KEY `ix_audit_access_logs_target_user_uid` (`target_user_uid`),
  KEY `ix_audit_access_logs_created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='会话查阅日志';
