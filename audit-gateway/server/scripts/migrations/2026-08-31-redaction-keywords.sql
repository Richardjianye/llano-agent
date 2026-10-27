-- 按部门设置的屏蔽词（阶段 18）
--
-- 执行方式：
--   mysql -u root -p audit_gateway < scripts/migrations/2026-08-31-redaction-keywords.sql
--
-- 形状照 model_whitelist / token_quotas：scope + scope_value，解析时取并集。
-- 照抄是有意的——后台已经有两个「按部门配」的页面，第三个长得不一样只会让人
-- 每次都要重新读一遍规则。
--
-- ⚠️ 与环境变量 MASK_KEYWORDS 是**相加不是替换**
--
--   环境变量那份继续作为全局基线：已经部署的实例不会因为这张表是空的就突然什么
--   都不遮。反过来（表存在就忽略环境变量）会造成一个很难查的失败：某天有人建了
--   第一条 dept 规则，全局那几个词**当场失效**，而没有任何一处会报错。
--
-- ⚠️ 群聊里按**发言人**的部门遮，不按读者
--
--   一条消息从 A 部门发出、被 B 部门读到。要防的是「A 的内部术语流到 B」，
--   所以生效的是发言人部门的词表。按读者遮是错的：那样 A 的术语在 B 眼里原样
--   呈现，恰恰是要防的那个方向。

CREATE TABLE IF NOT EXISTS `redaction_keywords` (
  `id` INT NOT NULL AUTO_INCREMENT COMMENT '自增ID',
  `scope` VARCHAR(16) NOT NULL COMMENT 'global / dept',
  `scope_value` VARCHAR(64) NOT NULL DEFAULT '' COMMENT '部门ID；global 为空串',
  `keyword` VARCHAR(128) NOT NULL COMMENT '要遮的词；匹配大小写不敏感',
  `status` INT NOT NULL DEFAULT 1 COMMENT '1启用 0停用',
  `remark` VARCHAR(255) NULL COMMENT '备注：为什么要遮它',
  `created_at` DATETIME NOT NULL COMMENT '创建时间',
  `updated_at` DATETIME NOT NULL COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_redaction_keyword` (`scope`, `scope_value`, `keyword`),
  KEY `ix_redaction_scope` (`scope`, `scope_value`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='按部门设置的屏蔽词，与 MASK_KEYWORDS 相加';

-- 新部署不预置屏蔽词；请通过管理后台按实际需要配置。
