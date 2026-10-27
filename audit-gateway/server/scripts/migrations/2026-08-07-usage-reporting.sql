-- 用量统计读取侧：新增列
--
-- 为什么需要这个文件：当前建表走 SQLAlchemy 的 create_all，它只会「建缺失的表」，
-- **不会给已存在的表加列**。所以已经跑过一次的库必须手工执行这段，
-- 否则启动不报错、写入时才炸在 Unknown column 上。
--
-- 全新的库不用管：create_all 会直接建成新结构。
-- （治本方案是引入 Alembic，已记在 README 的「已知待办」里。）
--
-- 执行：
--   mysql -u root -p audit_gateway < scripts/migrations/2026-08-07-usage-reporting.sql
--
-- 幂等性：MySQL 8.0 不支持 ADD COLUMN IF NOT EXISTS，重复执行会报
-- "Duplicate column name"。那个报错是安全的，说明这一列已经加过了。

-- ---------- 模型单价（报表估算费用用）----------
-- NULL 表示未配置，与 0（免费）语义不同：报表遇到 NULL 会把费用标成「无法估算」，
-- 而不是当成不花钱，避免总额悄悄偏小。
ALTER TABLE `models`
  ADD COLUMN `input_price_per_1k`  DECIMAL(12,6) NULL COMMENT '输入单价（元/千 token），NULL 为未配置',
  ADD COLUMN `output_price_per_1k` DECIMAL(12,6) NULL COMMENT '输出单价（元/千 token），NULL 为未配置',
  ADD COLUMN `currency` VARCHAR(8) NOT NULL DEFAULT 'CNY' COMMENT '计价货币';

-- ---------- 日聚合表：报表要的两个指标 ----------
-- 没有这几列，「平均耗时」和「估算占比」就只能回头扫明细表，
-- 那正是分出日聚合表本来要避免的事。
ALTER TABLE `usage_daily`
  ADD COLUMN `latency_ms_sum` BIGINT NOT NULL DEFAULT 0 COMMENT '耗时合计（毫秒），除以 requests 得均值',
  ADD COLUMN `estimated_requests` INT NOT NULL DEFAULT 0 COMMENT '其中 token 为估算值的请求数',
  ADD COLUMN `estimated_tokens` BIGINT NOT NULL DEFAULT 0 COMMENT '其中估算得来的 token 合计';

-- ---------- 明细表：token 细分与上游 usage 原文 ----------
-- 成本计算功能以后再做，但这些数字**这一刻不存就永远追不回来**。
-- usage_raw 是兜底：认不出来的字段（分时缓存 TTL 分档、按次计费、厂商私有扩展）
-- 都在里面，将来定价规则怎么变都能拿它重算，不必回头改写入侧。
ALTER TABLE `usage_records`
  ADD COLUMN `cached_prompt_tokens` INT NOT NULL DEFAULT 0 COMMENT '输入中命中缓存的 token',
  ADD COLUMN `cache_write_tokens`   INT NOT NULL DEFAULT 0 COMMENT '写入缓存的 token',
  ADD COLUMN `reasoning_tokens`     INT NOT NULL DEFAULT 0 COMMENT '推理模型的思考 token',
  ADD COLUMN `usage_raw` TEXT NULL COMMENT '上游返回的 usage 原文 JSON';

-- ---------- 报表查询用的复合索引 ----------
-- 报表几乎总是「先卡时间范围，再按部门/用户收窄」，
-- 单列索引在这种组合下选择性不够。
CREATE INDEX `idx_usage_daily_date_dept` ON `usage_daily` (`stat_date`, `dept_id`);
CREATE INDEX `idx_usage_daily_date_user` ON `usage_daily` (`stat_date`, `user_uid`);
CREATE INDEX `idx_usage_records_date_user` ON `usage_records` (`stat_date`, `user_uid`);
