-- 先迁移后部署。只统计实际使用，预览、扫描和编辑不进入本表。
CREATE TABLE IF NOT EXISTS `skill_hub_usage_events` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `user_uid` VARCHAR(36) NOT NULL,
  `event_id` VARCHAR(32) NOT NULL,
  `slug` VARCHAR(64) NOT NULL,
  `api_key_id` INT NULL,
  `created_at` DATETIME NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_skill_usage_user_event` (`user_uid`, `event_id`),
  KEY `ix_skill_usage_slug` (`slug`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
