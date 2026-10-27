-- 版本化公司提示词策略：唯一草稿、不可变发布版本、无正文操作审计。
--
-- 执行顺序：迁移先于 0.18.0 网关代码部署。首版发布前只有空草稿；客户端收到
-- /v1/prompt-policy/effective 的 404 后继续使用安装包内置基线。首版发布之后，
-- 网关才成为新会话的远端权威来源。

CREATE TABLE IF NOT EXISTS `prompt_policy_drafts` (
  `id` INT NOT NULL COMMENT '固定为 1',
  `company_core_policy` MEDIUMTEXT NOT NULL COMMENT '不可覆盖的公司核心规则草稿',
  `default_persona` MEDIUMTEXT NOT NULL COMMENT '默认人格草稿',
  `content_hash` VARCHAR(71) NULL COMMENT '规范化策略包 sha256',
  `revision` INT NOT NULL DEFAULT 0 COMMENT '草稿乐观锁版本',
  `base_version` BIGINT NULL COMMENT '草稿最近一次发布形成的版本',
  `updated_by_uid` VARCHAR(36) NULL,
  `updated_by_name` VARCHAR(64) NULL,
  `created_at` DATETIME NOT NULL,
  `updated_at` DATETIME NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='公司提示词策略唯一草稿'
;

CREATE TABLE IF NOT EXISTS `prompt_policy_versions` (
  `version` BIGINT NOT NULL COMMENT '单调递增版本号',
  `company_core_policy` MEDIUMTEXT NOT NULL,
  `default_persona` MEDIUMTEXT NOT NULL,
  `content_hash` VARCHAR(71) NOT NULL,
  `source_version` BIGINT NULL COMMENT '普通发布为空，回滚为被恢复版本',
  `published_at` DATETIME NOT NULL,
  `published_by_uid` VARCHAR(36) NOT NULL,
  `published_by_name` VARCHAR(64) NOT NULL,
  PRIMARY KEY (`version`),
  KEY `ix_prompt_policy_versions_hash` (`content_hash`),
  KEY `ix_prompt_policy_versions_published` (`published_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='不可变的已发布提示词策略版本'
;

CREATE TABLE IF NOT EXISTS `prompt_policy_audit_logs` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `action` VARCHAR(32) NOT NULL COMMENT 'draft_created/draft_updated/published/rolled_back',
  `actor_uid` VARCHAR(36) NOT NULL,
  `actor_name` VARCHAR(64) NOT NULL,
  `content_hash` VARCHAR(71) NOT NULL,
  `draft_revision` INT NULL,
  `version` BIGINT NULL,
  `source_version` BIGINT NULL,
  `created_at` DATETIME NOT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_prompt_policy_audit_created` (`created_at`),
  KEY `ix_prompt_policy_audit_action` (`action`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='提示词策略操作审计，仅存 hash 不存正文'
;

INSERT INTO `prompt_policy_drafts` (
  `id`, `company_core_policy`, `default_persona`, `content_hash`, `revision`,
  `base_version`, `updated_by_uid`, `updated_by_name`, `created_at`, `updated_at`
) VALUES (1, '', '', NULL, 0, NULL, NULL, NULL, NOW(), NOW())
ON DUPLICATE KEY UPDATE `id` = VALUES(`id`)
;
