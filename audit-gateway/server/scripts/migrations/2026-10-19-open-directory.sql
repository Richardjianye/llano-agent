-- 对外成员搜索（/open/v1/members/search）的调用审计表（2026-10-19）。先迁移，再部署。
--
-- 只新建一张空表，不动任何已有数据。接入应用复用 sso_clients（2026-10-14-sso-clients.sql），
-- 登记时 allowed_scopes 里带 directory.read 的应用才能换到令牌；那张表未建时接口一律
-- 返回 invalid_client，不影响网关其他功能。
CREATE TABLE IF NOT EXISTS `open_api_call_logs` (
  `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增ID',
  `client_id` VARCHAR(96) NOT NULL COMMENT '接入应用 client_id',
  `endpoint` VARCHAR(64) NOT NULL COMMENT '接口，如 members.search',
  `status` VARCHAR(16) NOT NULL COMMENT 'ok/denied/rate_limited/invalid',
  `query_digest` VARCHAR(64) NULL COMMENT '查询条件 sha256，便于比对重复查询',
  `query_summary` VARCHAR(255) NULL COMMENT '查询条件摘要（不含关键字原文）',
  `result_count` INT NOT NULL DEFAULT 0 COMMENT '返回条数',
  `client_ip` VARCHAR(64) NULL COMMENT '来源IP',
  `created_at` DATETIME NOT NULL COMMENT '发生时间',
  PRIMARY KEY (`id`),
  KEY `ix_open_api_call_logs_client_id` (`client_id`),
  KEY `ix_open_api_call_logs_created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
