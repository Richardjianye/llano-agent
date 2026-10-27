CREATE TABLE IF NOT EXISTS mcp_instances (
  id INT AUTO_INCREMENT PRIMARY KEY,
  code VARCHAR(64) NOT NULL UNIQUE,
  name VARCHAR(128) NOT NULL,
  adapter_key VARCHAR(64) NOT NULL,
  auth_config_encrypted TEXT NULL,
  config_json TEXT NULL,
  status INT NOT NULL DEFAULT 1,
  remark VARCHAR(255) NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  INDEX ix_mcp_instances_adapter_key (adapter_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS mcp_capabilities (
  id INT AUTO_INCREMENT PRIMARY KEY,
  instance_id INT NOT NULL,
  capability_code VARCHAR(128) NOT NULL,
  name VARCHAR(255) NOT NULL,
  domain VARCHAR(64) NOT NULL,
  description TEXT NULL,
  method VARCHAR(8) NOT NULL,
  path VARCHAR(512) NOT NULL,
  input_schema_json TEXT NULL,
  metadata_json TEXT NULL,
  source_hash VARCHAR(64) NOT NULL,
  status INT NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  CONSTRAINT uq_mcp_instance_capability UNIQUE (instance_id, capability_code),
  INDEX ix_mcp_capabilities_instance_id (instance_id),
  INDEX ix_mcp_capabilities_capability_code (capability_code),
  INDEX ix_mcp_capabilities_domain (domain)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS mcp_user_permissions (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_uid VARCHAR(36) NOT NULL,
  instance_id INT NOT NULL,
  capability_id INT NOT NULL,
  effect VARCHAR(8) NOT NULL DEFAULT 'allow',
  constraints_json TEXT NULL,
  expires_at DATETIME NULL,
  status INT NOT NULL DEFAULT 1,
  granted_by VARCHAR(36) NOT NULL,
  remark VARCHAR(255) NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  CONSTRAINT uq_mcp_user_capability UNIQUE (user_uid, instance_id, capability_id),
  INDEX ix_mcp_user_permissions_user_uid (user_uid),
  INDEX ix_mcp_user_permissions_instance_id (instance_id),
  INDEX ix_mcp_user_permissions_capability_id (capability_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS mcp_call_logs (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  request_id VARCHAR(64) NOT NULL,
  user_uid VARCHAR(36) NOT NULL,
  api_key_id INT NULL,
  instance_id INT NULL,
  capability_id INT NULL,
  capability_code VARCHAR(128) NOT NULL,
  args_json TEXT NULL,
  status VARCHAR(16) NOT NULL,
  error_code VARCHAR(64) NULL,
  upstream_request_id VARCHAR(128) NULL,
  latency_ms INT NOT NULL DEFAULT 0,
  response_size BIGINT NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL,
  INDEX ix_mcp_call_logs_request_id (request_id),
  INDEX ix_mcp_call_logs_user_uid (user_uid),
  INDEX ix_mcp_call_logs_instance_id (instance_id),
  INDEX ix_mcp_call_logs_capability_code (capability_code),
  INDEX ix_mcp_call_logs_status (status),
  INDEX ix_mcp_call_logs_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
