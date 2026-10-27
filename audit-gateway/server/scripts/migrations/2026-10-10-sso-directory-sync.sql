-- 在部署之前由 scripts/migrate.py 执行，禁止在生产服务启动后再补列。
ALTER TABLE users ADD COLUMN account_kind VARCHAR(16) NOT NULL DEFAULT 'internal';
ALTER TABLE users ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT 0;
ALTER TABLE external_identities ADD CONSTRAINT uq_external_identity_member UNIQUE (provider, tenant_id, provider_user_id);
CREATE TABLE IF NOT EXISTS directory_snapshots (
 id VARCHAR(36) PRIMARY KEY, provider VARCHAR(32) NOT NULL, tenant_id VARCHAR(128) NOT NULL,
 operator_uid VARCHAR(36) NOT NULL, created_at DATETIME(6) NOT NULL, completed_at DATETIME(6) NULL,
 complete BOOLEAN NOT NULL DEFAULT 0, payload MEDIUMTEXT NOT NULL,
 INDEX ix_directory_snapshots_tenant_id (tenant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS directory_operations (
 id INT NOT NULL AUTO_INCREMENT PRIMARY KEY, snapshot_id VARCHAR(36) NOT NULL,
 row_key VARCHAR(160) NOT NULL, action VARCHAR(16) NOT NULL, user_uid VARCHAR(36) NOT NULL,
 operator_uid VARCHAR(36) NOT NULL, created_at DATETIME NOT NULL,
 UNIQUE KEY uq_directory_operation_row (snapshot_id, row_key), INDEX ix_directory_operations_snapshot_id (snapshot_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
