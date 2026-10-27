-- OIDC 接入应用由总裁登记；先迁移，再配置独立 issuer/私钥并启用。
CREATE TABLE IF NOT EXISTS `sso_clients` (
  `client_id` VARCHAR(96) NOT NULL,
  `name` VARCHAR(128) NOT NULL,
  `owner_uid` VARCHAR(36) NOT NULL,
  `environment` VARCHAR(16) NOT NULL,
  `audit_contact` VARCHAR(128) NOT NULL,
  `client_type` VARCHAR(16) NOT NULL,
  `redirect_uris` TEXT NOT NULL,
  `allowed_scopes` VARCHAR(128) NOT NULL,
  `secret_hash` VARCHAR(64) NULL,
  `enabled` TINYINT(1) NOT NULL DEFAULT 1,
  `created_at` DATETIME NOT NULL,
  `updated_at` DATETIME NOT NULL,
  PRIMARY KEY (`client_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
