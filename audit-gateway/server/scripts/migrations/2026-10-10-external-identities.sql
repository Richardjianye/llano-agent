-- 在既有 users.uid 上建立跨平台身份映射。先部署此表，再启用钉钉登录。
CREATE TABLE IF NOT EXISTS `external_identities` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `provider` VARCHAR(32) NOT NULL,
  `tenant_id` VARCHAR(128) NOT NULL,
  `subject_id` VARCHAR(128) NOT NULL,
  `user_uid` VARCHAR(36) NOT NULL,
  `provider_user_id` VARCHAR(128) NULL,
  `created_at` DATETIME NOT NULL,
  `updated_at` DATETIME NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_external_identity_subject` (`provider`, `tenant_id`, `subject_id`),
  UNIQUE KEY `uq_external_identity_user` (`provider`, `tenant_id`, `user_uid`),
  KEY `ix_external_identities_user_uid` (`user_uid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
