-- 会话审计列表页的摘要改成「最后一条用户消息」，入库时落定。
--
-- 执行方式：
--   mysql -u root -p audit_gateway < scripts/migrations/2026-08-20-audit-last-user-preview.sql
--
-- 为什么要加这一列，而不是查询时现算：
--   `request_messages` 是 TEXT，一页 20 条能拉回几 MB，所以列表页原本用
--   SQL 的 LEFT() 只取开头一小段回来再拍平。但 Hermes agent 把系统提示排在
--   messages 数组第一个，取到的开头永远是那段每条记录都一样的提示词，
--   用户真正问了什么在数组结尾 —— LEFT() 一辈子够不着。
--
-- 老数据不回填：`request_messages` 在此之前是被「切 JSON 字符串」的方式截断的
--   （见 services/audit_content.py 的模块注释），大部分老行存的是半截 JSON，
--   解析不出可靠的用户消息，硬回填只会填进一堆系统提示。列表页对
--   last_user_preview 为空的行自动退回原来的取开头逻辑，效果和以前一样，不更差。

ALTER TABLE `conversation_logs`
  ADD COLUMN `last_user_preview` VARCHAR(512) NULL
    COMMENT '最后一条用户消息的摘要（列表页用，入库时落定）'
    AFTER `request_messages`;
