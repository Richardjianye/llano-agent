-- API Key 独立累计 Token 额度与按 Key 归集用量。
--
-- 执行方式：
--   mysql -u root -p audit_gateway < scripts/migrations/2026-08-13-api-key-quota.sql
--
-- 历史 usage_records 无法可靠反推出具体使用了哪一把 Key，因此 api_key_id 保持 NULL；
-- 独立额度从迁移完成后的新调用开始累计，不影响既有用户/部门日额度。

ALTER TABLE `api_keys`
  ADD COLUMN `total_token_limit` BIGINT NULL
    COMMENT '该 Key 的累计 token 上限，空为不单独限制'
    AFTER `expires_at`;

ALTER TABLE `usage_records`
  ADD COLUMN `api_key_id` INT NULL
    COMMENT '调用使用的 API Key，JWT 调用为空'
    AFTER `request_id`,
  ADD INDEX `ix_usage_records_api_key_id` (`api_key_id`);
