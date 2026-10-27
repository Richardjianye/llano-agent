-- API Key 吊销记录（2026-10-19）。先迁移，再部署：prod 的 warn_schema_drift 会拒绝启动。
--
-- 只加三列可空字段，不改任何已有行：存量已吊销的 Key 这三列为 NULL（原因未知），
-- 有效的 Key 不受影响。api_keys 生产约 560 行，ALTER 是瞬时的。
--
-- 闲置吊销不需要改表：判定用已有的 last_used_at / created_at，阈值在配置里
-- （API_KEY_IDLE_DAYS，默认 30 天）。
--
-- ⚠️ 对生产数据的影响：部署后约 60 秒，后台巡检会把闲置超过 30 天的个人 Key
-- 吊销（revoke_reason=idle_30d, revoked_by=system）。2026-10-19 只读核对时为 5 把，
-- 无超过 60 天的。被吊销的客户端在下次打开时提示重新登录 —— 这些客户端的
-- refresh token 早已过期（7 天），本来也必须重新登录。
ALTER TABLE `api_keys`
  ADD COLUMN `revoked_at` DATETIME NULL COMMENT '吊销时间' AFTER `last_used_at`,
  ADD COLUMN `revoke_reason` VARCHAR(32) NULL COMMENT 'client_logout/admin/admin_batch/idle_<N>d/account_disabled/password_reset/self_password_change' AFTER `revoked_at`,
  ADD COLUMN `revoked_by` VARCHAR(36) NULL COMMENT '操作者uid；客户端自助吊销为空' AFTER `revoke_reason`;
