-- 用户管理页按本页 UID 查询最近一次真实模型请求。
-- user_uid / call_kind 单列索引只能分别过滤；组合索引可以直接定位每个用户的
-- chat 请求，并从索引尾部取得最近时间。

ALTER TABLE `usage_records`
  ADD INDEX `idx_usage_records_user_kind_created` (`user_uid`, `call_kind`, `created_at`);
