-- 静态产物元数据。文件本体位于 ARTIFACT_STORAGE_PATH 私有持久卷。
CREATE TABLE IF NOT EXISTS `published_sites` (
  `id` VARCHAR(36) COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
  `title` VARCHAR(128) NOT NULL,
  `owner_uid` VARCHAR(36) NOT NULL,
  `visibility` VARCHAR(16) NOT NULL DEFAULT 'private',
  `allowed_users` TEXT NOT NULL,
  `allowed_depts` TEXT NOT NULL,
  `status` VARCHAR(16) NOT NULL DEFAULT 'draft',
  `latest_version` INT NOT NULL DEFAULT 0,
  `published_version` INT NULL,
  `created_at` DATETIME NOT NULL,
  `updated_at` DATETIME NOT NULL,
  KEY `ix_published_sites_owner_uid` (`owner_uid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS `published_site_versions` (
  `id` INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  `site_id` VARCHAR(36) COLLATE utf8mb4_bin NOT NULL,
  `version` INT NOT NULL,
  `source_kind` VARCHAR(16) NOT NULL,
  `files` TEXT NOT NULL,
  `bytes_total` BIGINT NOT NULL,
  `created_by` VARCHAR(36) NOT NULL,
  `created_at` DATETIME NOT NULL,
  UNIQUE KEY `uq_published_site_version` (`site_id`, `version`),
  KEY `ix_published_site_versions_site_id` (`site_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
