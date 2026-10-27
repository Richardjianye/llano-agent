-- 容器时区：UTC → Asia/Shanghai，存量时间戳补上那 8 小时
--
-- 执行方式：
--   mysql -u root -p audit_gateway < scripts/migrations/2026-09-03-container-timezone.sql
--
-- ## ⚠️ 跑之前先备份
--
--   mysqldump -u root -p audit_gateway > backup-$(date +%F).sql
--
-- 重复执行会把时间再往后推 8 小时。这**不是假想的风险**：2026-09-03 首次执行时
-- 就真跑了两遍（第一遍的输出被一个 grep 吞掉，看上去像没执行），全库变成 +16
-- 小时，最后是靠备份逐行重算回来的。
--
-- 所以下面加了一道自锁：schema_migrations 里已经有这一行就整份空转。
-- migrate.py 是跑完才记录，首次执行不受影响；手工重跑则不会再叠加。
-- **备份仍然要做** —— 自锁挡的是重复执行，挡不住「跑错了库」。
--
-- ## 为什么会有这份
--
-- `model/database.now()` 的契约是「服务器本地时间」，注释里写着「部署时保证服务器
-- 时区为 Asia/Shanghai 即可」—— 但那句话没有任何一处代码或配置在保证它：
-- Dockerfile 从没设过 TZ，Zeabur 的环境变量里也没有。容器一直跑在 UTC，于是
-- **写进库里的全是 UTC**，后台每一处时间都比实际早 8 小时。
--
-- 2026-08-08 那份迁移做过同样的搬移，但它是 2026-08-31 02:35 跑的，当时这几张表
-- 还是空的（最早的业务数据是 2026-09-01 10:25），所以它一行都没搬到。
-- 换句话说：**库里现在是清一色的 UTC，没有新旧混杂**，这也是这份能安全整表搬的前提。
--
-- 真正的修法在 Dockerfile（ENV TZ=Asia/Shanghai）与部署环境变量，这份只负责存量。
--
-- ## 关于 cutoff
--
-- 环境变量改完要重启才生效，重启之后写入的就已经是本地时间了，不能再 +8。
-- 所以每一列都带 `<= '2026-09-03 10:41:29'` 这个闸门（重启那一刻的 UTC 时刻）：
--
--   · 重启前写的值 <= cutoff        → 搬
--   · 重启后写的值 >= cutoff + 8h   → 不动
--
-- 两者之间有整整 8 小时的安全间隔，不会误判。**先改环境变量并重启，再跑这份**。
--
-- ## stat_date 不用动
--
-- +8 之后没有任何一行跨日（全库时间戳落在 UTC 00:00–11:00，即本地 08:00–19:00，
-- 大家都在白天用），所以 usage_records.stat_date 与 usage_daily 的分桶依旧正确。
-- 执行前可以自己复核一遍：
--
--   SELECT COUNT(*) FROM usage_records WHERE TIME(created_at) >= '16:00:00';
--
-- 不是 0 的话，说明有记录会跨日，那 stat_date 得跟着重算、usage_daily 要重新
-- 聚合 —— 别硬跑这份。

SET @cutoff = '2026-09-03 10:41:29';

-- 自锁：跑过一次之后 @cutoff 置空，下面每一条 `<= @cutoff` 都不再命中任何行
-- （与 NULL 比较的结果是 NULL，不是 TRUE）。整份变成空转，而不是再叠 8 小时。
SET @cutoff = IF(
  (SELECT COUNT(*) FROM `schema_migrations`
    WHERE `filename` = '2026-09-03-container-timezone.sql') > 0,
  NULL, @cutoff);

-- 「现在」类时间戳：逐列判断而不是按行判断。
-- 一行可能创建于重启前、更新于重启后（created_at 要搬，updated_at 不能搬），
-- 按行判断会把这种行整体搬错。
UPDATE `api_keys`               SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `api_keys`               SET `updated_at`      = DATE_ADD(`updated_at`,      INTERVAL 8 HOUR) WHERE `updated_at`      <= @cutoff;
UPDATE `api_keys`               SET `last_used_at`    = DATE_ADD(`last_used_at`,    INTERVAL 8 HOUR) WHERE `last_used_at`    <= @cutoff;
UPDATE `audit_access_logs`      SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `client_releases`        SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `client_releases`        SET `updated_at`      = DATE_ADD(`updated_at`,      INTERVAL 8 HOUR) WHERE `updated_at`      <= @cutoff;
UPDATE `conversation_attachments` SET `created_at`    = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `conversation_logs`      SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `credential_access_logs` SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `departments`            SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `departments`            SET `updated_at`      = DATE_ADD(`updated_at`,      INTERVAL 8 HOUR) WHERE `updated_at`      <= @cutoff;
UPDATE `mcp_call_logs`          SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `mcp_capabilities`       SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `mcp_capabilities`       SET `updated_at`      = DATE_ADD(`updated_at`,      INTERVAL 8 HOUR) WHERE `updated_at`      <= @cutoff;
UPDATE `mcp_instances`          SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `mcp_instances`          SET `updated_at`      = DATE_ADD(`updated_at`,      INTERVAL 8 HOUR) WHERE `updated_at`      <= @cutoff;
UPDATE `mcp_user_permissions`   SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `mcp_user_permissions`   SET `updated_at`      = DATE_ADD(`updated_at`,      INTERVAL 8 HOUR) WHERE `updated_at`      <= @cutoff;
UPDATE `model_whitelist`        SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `model_whitelist`        SET `updated_at`      = DATE_ADD(`updated_at`,      INTERVAL 8 HOUR) WHERE `updated_at`      <= @cutoff;
UPDATE `models`                 SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `models`                 SET `updated_at`      = DATE_ADD(`updated_at`,      INTERVAL 8 HOUR) WHERE `updated_at`      <= @cutoff;
UPDATE `platform_credentials`   SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `platform_credentials`   SET `updated_at`      = DATE_ADD(`updated_at`,      INTERVAL 8 HOUR) WHERE `updated_at`      <= @cutoff;
UPDATE `platform_credentials`   SET `last_verified_at`= DATE_ADD(`last_verified_at`,INTERVAL 8 HOUR) WHERE `last_verified_at`<= @cutoff;
UPDATE `project_members`        SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `project_members`        SET `updated_at`      = DATE_ADD(`updated_at`,      INTERVAL 8 HOUR) WHERE `updated_at`      <= @cutoff;
UPDATE `project_members`        SET `joined_at`       = DATE_ADD(`joined_at`,       INTERVAL 8 HOUR) WHERE `joined_at`       <= @cutoff;
UPDATE `project_members`        SET `left_at`         = DATE_ADD(`left_at`,         INTERVAL 8 HOUR) WHERE `left_at`         <= @cutoff;
UPDATE `project_messages`       SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `project_tasks`          SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `project_tasks`          SET `updated_at`      = DATE_ADD(`updated_at`,      INTERVAL 8 HOUR) WHERE `updated_at`      <= @cutoff;
UPDATE `project_tasks`          SET `claimed_at`      = DATE_ADD(`claimed_at`,      INTERVAL 8 HOUR) WHERE `claimed_at`      <= @cutoff;
UPDATE `project_tasks`          SET `confirmed_at`    = DATE_ADD(`confirmed_at`,    INTERVAL 8 HOUR) WHERE `confirmed_at`    <= @cutoff;
UPDATE `project_tasks`          SET `done_at`         = DATE_ADD(`done_at`,         INTERVAL 8 HOUR) WHERE `done_at`         <= @cutoff;
UPDATE `projects`               SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `projects`               SET `updated_at`      = DATE_ADD(`updated_at`,      INTERVAL 8 HOUR) WHERE `updated_at`      <= @cutoff;
UPDATE `projects`               SET `archived_at`     = DATE_ADD(`archived_at`,     INTERVAL 8 HOUR) WHERE `archived_at`     <= @cutoff;
UPDATE `redaction_keywords`     SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `redaction_keywords`     SET `updated_at`      = DATE_ADD(`updated_at`,      INTERVAL 8 HOUR) WHERE `updated_at`      <= @cutoff;
UPDATE `schema_migrations`      SET `applied_at`      = DATE_ADD(`applied_at`,      INTERVAL 8 HOUR) WHERE `applied_at`      <= @cutoff;
UPDATE `skill_hub_skills`       SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `skill_hub_skills`       SET `updated_at`      = DATE_ADD(`updated_at`,      INTERVAL 8 HOUR) WHERE `updated_at`      <= @cutoff;
UPDATE `skill_hub_skills`       SET `promoted_at`     = DATE_ADD(`promoted_at`,     INTERVAL 8 HOUR) WHERE `promoted_at`     <= @cutoff;
UPDATE `skill_hub_skills`       SET `delisted_at`     = DATE_ADD(`delisted_at`,     INTERVAL 8 HOUR) WHERE `delisted_at`     <= @cutoff;
UPDATE `skill_hub_versions`     SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `skill_security_events`  SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `skill_security_samples` SET `first_seen_at`   = DATE_ADD(`first_seen_at`,   INTERVAL 8 HOUR) WHERE `first_seen_at`   <= @cutoff;
UPDATE `skill_security_samples` SET `last_seen_at`    = DATE_ADD(`last_seen_at`,    INTERVAL 8 HOUR) WHERE `last_seen_at`    <= @cutoff;
UPDATE `token_quotas`           SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `token_quotas`           SET `updated_at`      = DATE_ADD(`updated_at`,      INTERVAL 8 HOUR) WHERE `updated_at`      <= @cutoff;
UPDATE `upstream_providers`     SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `upstream_providers`     SET `updated_at`      = DATE_ADD(`updated_at`,      INTERVAL 8 HOUR) WHERE `updated_at`      <= @cutoff;
UPDATE `usage_daily`            SET `updated_at`      = DATE_ADD(`updated_at`,      INTERVAL 8 HOUR) WHERE `updated_at`      <= @cutoff;
UPDATE `usage_records`          SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `users`                  SET `created_at`      = DATE_ADD(`created_at`,      INTERVAL 8 HOUR) WHERE `created_at`      <= @cutoff;
UPDATE `users`                  SET `updated_at`      = DATE_ADD(`updated_at`,      INTERVAL 8 HOUR) WHERE `updated_at`      <= @cutoff;
UPDATE `users`                  SET `last_login_at`   = DATE_ADD(`last_login_at`,   INTERVAL 8 HOUR) WHERE `last_login_at`   <= @cutoff;

-- 「未来」类时间戳按所属行的 created_at 判断：它们本来就大于 cutoff，
-- 用自己的值当闸门永远不会命中。
UPDATE `api_keys`             SET `expires_at` = DATE_ADD(`expires_at`, INTERVAL 8 HOUR)
 WHERE `expires_at` IS NOT NULL AND `created_at` <= DATE_ADD(@cutoff, INTERVAL 8 HOUR);
UPDATE `mcp_user_permissions` SET `expires_at` = DATE_ADD(`expires_at`, INTERVAL 8 HOUR)
 WHERE `expires_at` IS NOT NULL AND `created_at` <= DATE_ADD(@cutoff, INTERVAL 8 HOUR);
