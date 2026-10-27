-- ① 时区口径统一为服务器本地时间　② 模型单价改为「元 / M Token」
--
-- ⚠️ **按文件名顺序跑。** 前置：2026-08-08-conversation-audit.sql
--    后续：2026-08-08-billing-modes-and-abort.sql
--    跳过本份直接跑下一份，会让 models 表停在「新列已加、旧列名未改」的
--    半新半旧状态，服务照常启动但一转发就 500。（已踩过一次）
--
-- 执行：
--   mysql -u root -p audit_gateway < scripts/migrations/2026-08-08-timezone-and-pricing.sql
--
-- ⚠️ 这份迁移会**改写存量数据**（时间戳 +8 小时、单价 ×1000），
-- 与前面几份纯加列的迁移不同，**跑之前先备份**：
--   mysqldump -u root -p audit_gateway > backup-$(date +%F).sql
-- 重复执行会把数据改错（再 +8、再 ×1000），务必只跑一次。

-- ============================================================
-- ① 时间戳：UTC → 服务器本地时间（东八区 +8 小时）
-- ============================================================
--
-- 问题：ORM 的时间戳默认值原本走 utcnow() 存 UTC，而用量统计的 stat_date
-- 走 date.today() 存服务器本地日。两套口径混在一个库里：
--
--   · 审计页显示的时间比实际早 8 小时（已在实测日志里看到 07:18 实为 15:18）
--   · 每天 00:00~08:00 的调用，用量报表算「今天」、审计记录写「昨天」，
--     两个页面对同一批请求给出不同日期
--
-- 代码侧已统一成服务器本地时间（model/database.py 的 now()）。
-- 存量数据也要跟着搬，否则新旧记录混在一张表里、时间线是断的。
--
-- ⚠️ 如果你的服务器时区**不是** Asia/Shanghai，把下面的 8 改成实际时差；
-- 若服务器本来就跑在 UTC 且你打算继续用 UTC，则整段跳过（但要相应地
-- 把部署时区固定下来，别让两台机器给出不同口径）。
--
-- 只搬「代码写入的时间戳」。stat_date 本来就是本地日，不要动。

UPDATE `conversation_logs`      SET `created_at` = DATE_ADD(`created_at`, INTERVAL 8 HOUR);
UPDATE `conversation_attachments` SET `created_at` = DATE_ADD(`created_at`, INTERVAL 8 HOUR);
UPDATE `audit_access_logs`      SET `created_at` = DATE_ADD(`created_at`, INTERVAL 8 HOUR);
UPDATE `usage_records`          SET `created_at` = DATE_ADD(`created_at`, INTERVAL 8 HOUR);
UPDATE `usage_daily`            SET `updated_at` = DATE_ADD(`updated_at`, INTERVAL 8 HOUR);
UPDATE `credential_access_logs` SET `created_at` = DATE_ADD(`created_at`, INTERVAL 8 HOUR);

UPDATE `users` SET
  `created_at`    = DATE_ADD(`created_at`,    INTERVAL 8 HOUR),
  `updated_at`    = DATE_ADD(`updated_at`,    INTERVAL 8 HOUR),
  `last_login_at` = DATE_ADD(`last_login_at`, INTERVAL 8 HOUR);

UPDATE `departments` SET
  `created_at` = DATE_ADD(`created_at`, INTERVAL 8 HOUR),
  `updated_at` = DATE_ADD(`updated_at`, INTERVAL 8 HOUR);

UPDATE `upstream_providers` SET
  `created_at` = DATE_ADD(`created_at`, INTERVAL 8 HOUR),
  `updated_at` = DATE_ADD(`updated_at`, INTERVAL 8 HOUR);

UPDATE `models` SET
  `created_at` = DATE_ADD(`created_at`, INTERVAL 8 HOUR),
  `updated_at` = DATE_ADD(`updated_at`, INTERVAL 8 HOUR);

UPDATE `model_whitelist` SET
  `created_at` = DATE_ADD(`created_at`, INTERVAL 8 HOUR),
  `updated_at` = DATE_ADD(`updated_at`, INTERVAL 8 HOUR);

-- API Key 的有效期也是用同一个函数算出来的，一起搬，
-- 否则改完之后这批 Key 会提前 8 小时失效
UPDATE `api_keys` SET
  `expires_at`   = DATE_ADD(`expires_at`,   INTERVAL 8 HOUR),
  `last_used_at` = DATE_ADD(`last_used_at`, INTERVAL 8 HOUR);

UPDATE `platform_credentials` SET
  `created_at` = DATE_ADD(`created_at`, INTERVAL 8 HOUR),
  `updated_at` = DATE_ADD(`updated_at`, INTERVAL 8 HOUR);


-- ============================================================
-- ② 模型单价：元/千 token → 元/M token
-- ============================================================
--
-- 业内报价（DeepSeek、OpenAI、Anthropic）统一用「每百万 token」，
-- 后台却按千 token 填，每次录入都要先心算除以 1000 —— 这种地方迟早会错一个数量级，
-- 而且错了之后报表上的费用看起来仍然「像个正常数字」，很难发现。
--
-- 换算：1 元/千 token = 1000 元/M token。
-- NULL 保持 NULL（未配置 ≠ 免费，报表要显示「—」而不是 0）。

ALTER TABLE `models`
  CHANGE COLUMN `input_price_per_1k`  `input_price_per_1m`  DECIMAL(12,6) NULL COMMENT '输入单价（元/百万 token），NULL 为未配置',
  CHANGE COLUMN `output_price_per_1k` `output_price_per_1m` DECIMAL(12,6) NULL COMMENT '输出单价（元/百万 token），NULL 为未配置';

UPDATE `models` SET
  `input_price_per_1m`  = `input_price_per_1m`  * 1000
WHERE `input_price_per_1m` IS NOT NULL;

UPDATE `models` SET
  `output_price_per_1m` = `output_price_per_1m` * 1000
WHERE `output_price_per_1m` IS NOT NULL;

-- 校验：跑完之后确认单价落在合理量级（DeepSeek 类模型大致 0.5~16 元/M）
-- SELECT model_name, input_price_per_1m, output_price_per_1m, currency FROM models;
