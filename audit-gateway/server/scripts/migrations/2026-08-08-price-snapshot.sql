-- 单价快照：让历史费用不再随调价漂移
--
-- ⚠️⚠️ **必须按文件名顺序跑，这份排在最后。** 前置：
--      2026-08-07-usage-reporting.sql
--   →  2026-08-08-conversation-audit.sql
--   →  2026-08-08-timezone-and-pricing.sql
--   →  2026-08-08-billing-modes-and-abort.sql
--   →  本文件
--
-- 执行：
--   mysql -u root -p audit_gateway < scripts/migrations/2026-08-08-price-snapshot.sql
--
-- 这份**只加列，不改写任何存量数据**（新列一律给「未知」默认值），
-- 因此比 timezone-and-pricing 那份温和得多，重跑的唯一后果是
-- "Duplicate column name" 报错 —— 那个报错是安全的。
--
--
-- ## 解决的问题
--
-- 在这份迁移之前，报表里的费用是**拿今天的单价去乘历史的 token**。
-- 后果不是「数字不准」这么轻描淡写：
--
--   厂商 9 月把某模型降价一半 → 后台照着新价目表改了单价
--   → 8 月那份已经发出去的报表，今天再打开，金额直接少一半
--   → 谁也说不清当初那个数是怎么来的，与厂商账单更是永远对不上
--
-- 修法是把「这次调用当时按什么价算的」当场记下来：
-- `usage_records` 存单价与算出的费用，`usage_daily` 存费用合计。
-- 之后报表**优先用快照**，只有快照缺失（也就是这份迁移之前的历史数据）
-- 才退回按当前单价回算，并在响应里用 cost_basis 标出来是哪种。
--
--
-- ## 为什么不回填历史
--
-- 回填只能拿「今天的单价」去补，那正是要摆脱的东西 —— 补出来的快照
-- 会伪装成「当时的价」，比留空更糟：留空至少还能诚实地标成「按当前单价回算」。
--
-- 真要精算历史，原料在 `usage_records.usage_raw`（上游 usage 原文全量入库，
-- 见决策 #21），拿到厂商月度账单后按账单口径重算即可。

-- ============================================================
-- ① 明细表：记下这次调用当时的单价与费用
-- ============================================================
--
-- cost 用 NULL 表示「当时就没配单价，算不出」，与 0.000000（确实是 0 元）
-- 是两回事 —— 这正是决策 #22「单价未配置时显示 —— 而绝不按 0 计入」在明细层的落点。
--
-- DECIMAL(18,6) 而不是 FLOAT：金额算术不能有二进制浮点误差。
-- 6 位小数是因为单次调用的费用常在「厘」以下（1000 token × 1元/百万 = 0.001 元）。

ALTER TABLE `usage_records`
  ADD COLUMN `billing_mode_snapshot` VARCHAR(16) NULL COMMENT '调用时的计价模式 token/per_call，NULL 为无快照',
  ADD COLUMN `input_price_per_1m_snapshot` DECIMAL(12,6) NULL COMMENT '调用时的输入单价（元/百万 token）',
  ADD COLUMN `output_price_per_1m_snapshot` DECIMAL(12,6) NULL COMMENT '调用时的输出单价（元/百万 token）',
  ADD COLUMN `cached_input_price_per_1m_snapshot` DECIMAL(12,6) NULL COMMENT '调用时的缓存命中输入单价，留空表示当时就按输入单价计',
  ADD COLUMN `price_per_call_snapshot` DECIMAL(12,6) NULL COMMENT '调用时的按次单价（元/次）',
  ADD COLUMN `cost` DECIMAL(18,6) NULL COMMENT '本次调用费用（按当时单价），NULL = 当时未配单价，算不出',
  ADD COLUMN `cost_currency` VARCHAR(8) NULL COMMENT '费用货币，跟随调用时的模型配置';

-- ============================================================
-- ② 日聚合表：费用合计 + 两个计数器
-- ============================================================
--
-- 报表查的是日聚合表，所以费用也得在这里能直接取到，否则算费用又要回头扫明细
-- （那正是当初分出日聚合表要避免的事）。
--
-- 两个计数器是**判断这一行能不能用快照**的依据，缺一不可：
--
--   snapshot_requests —— 其中有多少笔带了单价快照。
--       它小于 requests，说明这一行混进了本次迁移之前的调用，
--       快照合计是残缺的，整行只能退回按当前单价回算。
--       跨越迁移当天的那一行会命中这种情况，第二天起自然恢复正常。
--
--   priced_requests —— 其中有多少笔真的算出了费用。
--       它小于 snapshot_requests，说明有调用当时压根没配单价，
--       费用算不全 → 整组报 NULL，绝不按 0 混进总额。
--
-- 存量行两个计数器都是 0（默认值），于是一律 snapshot_requests < requests，
-- 正好落进「按当前单价回算」那条分支 —— 与迁移前的行为完全一致，
-- 不需要为历史数据做任何特殊处理。

ALTER TABLE `usage_daily`
  ADD COLUMN `cost_sum` DECIMAL(18,6) NOT NULL DEFAULT 0 COMMENT '已定价请求的费用合计（按调用时单价）',
  ADD COLUMN `snapshot_requests` INT NOT NULL DEFAULT 0 COMMENT '其中带单价快照的请求数，小于 requests 即快照残缺',
  ADD COLUMN `priced_requests` INT NOT NULL DEFAULT 0 COMMENT '其中真正算出了费用的请求数，小于快照数即费用不完整',
  ADD COLUMN `cost_currency` VARCHAR(8) NULL COMMENT '费用货币';

-- 校验：跑完之后新发生的调用应当 snapshot_requests = requests
--
-- SELECT stat_date, user_uid, model_name,
--        requests, snapshot_requests, priced_requests, cost_sum, cost_currency
--   FROM `usage_daily`
--  ORDER BY stat_date DESC, id DESC
--  LIMIT 20;
--
-- SELECT request_id, model_name, prompt_tokens, completion_tokens,
--        billing_mode_snapshot, input_price_per_1m_snapshot,
--        output_price_per_1m_snapshot, cost, cost_currency
--   FROM `usage_records`
--  ORDER BY id DESC
--  LIMIT 20;
