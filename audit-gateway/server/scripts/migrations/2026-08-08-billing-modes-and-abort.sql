-- ① 中断标记与流式标记　② 计价模式（缓存单价 / 按次）　③ 多模态字段改名
--
-- ⚠️⚠️ **必须按文件名顺序跑，这份是最后一份。** 前置：
--      2026-08-07-usage-reporting.sql
--   →  2026-08-08-conversation-audit.sql
--   →  2026-08-08-timezone-and-pricing.sql
--   →  本文件
--
-- 跳着跑不会报错（每份单独执行都能成功），但会留下一个**半新半旧**的库：
-- 比如先跑本份、跳过 timezone-and-pricing，`models` 表就会同时存在
-- 新加的 billing_mode 和旧名的 input_price_per_1k —— 而 ORM 要的是
-- input_price_per_1m。结果是服务照常启动，一转发就 500，
-- 且报错藏在网关内部，从客户端看只是「响应里没有 usage」。（已踩过一次）
--
-- 执行：
--   mysql -u root -p audit_gateway < scripts/migrations/2026-08-08-billing-modes-and-abort.sql
--
-- 这份是加列 + 改列名，不改写业务数据（只把 supports_vision 的值原样搬到新列名下），
-- 比上一份温和。但仍然建议先备份。
--
-- 幂等性：MySQL 8.0 不支持 ADD COLUMN IF NOT EXISTS，重复执行会报
-- "Duplicate column name"，那个报错是安全的。

-- ============================================================
-- ① 中断与流式标记
-- ============================================================
--
-- `aborted` 与 `status` 是**正交**的两件事，所以必须单开一列。
--
-- 起因：实测里一条请求「显示有回复却标记为失败，错误码 client_aborted」。
-- 用户点「停止生成」时，这次调用真的发生了、token 真的花了、内容也真的产出了，
-- 只是没人看完 —— 它是一次**成功**的调用。
--
-- 把它写进 status 会污染「失败率」：那个指标是用来判断合作厂商上游稳不稳的，
-- 掺进用户主动叫停之后就失去意义了。

ALTER TABLE `usage_records`
  ADD COLUMN `aborted` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '客户端是否中途断开（与 status 正交）';

ALTER TABLE `conversation_logs`
  ADD COLUMN `stream`  TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否流式请求',
  ADD COLUMN `aborted` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '客户端是否中途断开';

-- 修正存量：之前那版把中断写成了 status='client_aborted'，搬到新列上。
-- 这些调用本身是成功的，status 一并纠正回 success。
UPDATE `usage_records`
   SET `aborted` = 1,
       `status` = 'success',
       `error_code` = NULL
 WHERE `status` = 'client_aborted' OR `error_code` = 'client_aborted';

UPDATE `conversation_logs`
   SET `aborted` = 1,
       `status` = 'success'
 WHERE `status` = 'client_aborted';

-- 存量的流式标记补一下：conversation_logs 之前没有这一列，
-- 从同一次请求的 usage_records 里搬过来（靠 request_id 对应）
UPDATE `conversation_logs` c
  JOIN `usage_records` u ON u.`request_id` = c.`request_id`
   SET c.`stream` = u.`stream`;

-- 日聚合表里被错记成失败的那部分也要纠正。
-- ⚠️ 这段只在「确实出现过 client_aborted」时才需要跑；
-- 由于日聚合是累加值、无法逐笔回滚，最稳妥的做法是**重算受影响的那几天**：
--   1) 先看哪些天受影响：
--      SELECT DISTINCT stat_date FROM usage_records WHERE aborted = 1;
--   2) 删掉那几天的聚合行，再由下面的语句从明细重建
-- 若你的库里 aborted 全为 0（没人中途停过），整段可以跳过。
--
-- DELETE FROM `usage_daily` WHERE `stat_date` IN (SELECT DISTINCT `stat_date` FROM `usage_records` WHERE `aborted` = 1);
--
-- INSERT INTO `usage_daily`
--   (stat_date, user_uid, dept_id, role, model_id, model_name,
--    requests, success_count, failed_count,
--    prompt_tokens, completion_tokens, total_tokens,
--    latency_ms_sum, estimated_requests, estimated_tokens, updated_at)
-- SELECT stat_date, user_uid, MAX(dept_id), MAX(role), model_id, MAX(model_name),
--        COUNT(*),
--        SUM(status = 'success'),
--        SUM(status <> 'success'),
--        SUM(prompt_tokens), SUM(completion_tokens), SUM(total_tokens),
--        SUM(latency_ms), SUM(tokens_estimated),
--        SUM(CASE WHEN tokens_estimated THEN total_tokens ELSE 0 END), NOW()
--   FROM `usage_records`
--  WHERE stat_date IN (SELECT DISTINCT stat_date FROM usage_records WHERE aborted = 1)
--  GROUP BY stat_date, user_uid, model_id;


-- ============================================================
-- ①b 日聚合表补缓存 token 列
-- ============================================================
--
-- 按缓存单价计价，报表层必须能拿到「其中有多少 token 是命中缓存的」。
-- 这个数只存在明细表的话，算费用就得回头扫 usage_records ——
-- 那正是当初分出日聚合表要避免的事（明细表随调用量线性增长）。

ALTER TABLE `usage_daily`
  ADD COLUMN `cached_prompt_tokens` BIGINT NOT NULL DEFAULT 0 COMMENT '其中命中缓存的输入 token 合计';

-- 存量回填：从明细表按 (日期, 用户, 模型) 汇总补上。
-- 不回填的话，历史那几天的缓存 token 恒为 0，会按全价计费而金额虚高。
UPDATE `usage_daily` d
  JOIN (
    SELECT stat_date, user_uid, model_id, SUM(cached_prompt_tokens) AS cached
      FROM `usage_records`
     GROUP BY stat_date, user_uid, model_id
  ) r
    ON  r.stat_date = d.stat_date
    AND r.user_uid  = d.user_uid
    AND (r.model_id = d.model_id OR (r.model_id IS NULL AND d.model_id IS NULL))
   SET d.`cached_prompt_tokens` = r.cached;


-- ============================================================
-- ② 计价模式：缓存读取单价 + 按次计费
-- ============================================================
--
-- ⚠️ 这一步**部分修订了决策 #21**（原文：「不自建计费引擎，缓存读写、按次、
-- 阶梯、分时缓存等复杂计价一律不做」）。修订范围仅限：缓存读取单价、按次计价。
-- **阶梯 / 分段 / 分时缓存仍然不做。**
--
-- 前置条件：缓存单价只有在上游确实返回缓存 token 数时才有意义。
-- 填之前先跑 `python scripts/probe_upstream_usage.py` 探明 ——
-- 上游不返回的话，配了单价也没有 token 数可乘，报表会一直按全价算，
-- 金额虚高且没人会察觉。

ALTER TABLE `models`
  ADD COLUMN `billing_mode` VARCHAR(16) NOT NULL DEFAULT 'token' COMMENT '计价模式 token/per_call',
  -- 留空 = 「没单独配」，计算时**回退到输入单价**，不按 0 算。
  -- 按 0 算等于宣称「缓存不要钱」，会让费用凭空偏低。
  ADD COLUMN `cached_input_price_per_1m` DECIMAL(12,6) NULL COMMENT '缓存命中输入单价（元/百万 token），留空则按输入单价计',
  ADD COLUMN `price_per_call` DECIMAL(12,6) NULL COMMENT '按次单价（元/次），billing_mode=per_call 时生效';


-- ============================================================
-- ③ supports_vision → supports_multimodal
-- ============================================================
--
-- 这个字段管的从来就不只是图片：豆包这类模型的视频、文档、音频走的是
-- 同一条 content parts 通道（都用 base64 内联），叫「支持图片」会让人
-- 以为传音频要另开一个开关。
-- CHANGE COLUMN 会保留原有取值，不用额外搬数据。

ALTER TABLE `models`
  CHANGE COLUMN `supports_vision` `supports_multimodal` TINYINT(1) NOT NULL DEFAULT 0
  COMMENT '是否支持多模态输入（图片/音频/视频/文档）';

-- 校验
-- SELECT model_name, supports_multimodal, billing_mode,
--        input_price_per_1m, output_price_per_1m,
--        cached_input_price_per_1m, price_per_call, currency
--   FROM models;
