-- 审计只留「问了什么 / 答了什么」（决策 #52 · 内容模型）
--
-- 执行方式：
--   mysql -u root -p audit_gateway < scripts/migrations/2026-08-23-audit-content-model.sql
--
-- 换掉的是 `request_messages` 那一列（整个 messages 数组的 JSON）。两个理由：
--   1. 系统提示词每条记录一模一样、员工也改不了，全文入库纯占体积。
--      审计对它唯一会问的问题是「这些记录用的是不是同一版提示词」，
--      一个 sha256 就够回答
--   2. 它排在 messages 数组第一个，把用户真正问的那句话挤出了 20000 字额度。
--      现在用户输入独占一列、独占额度，正常提问根本够不着上限
--
-- ⚠️ 知情取舍（负责人 2026-08-21 拍板）：本地工具的结果是以 tool 消息进模型
--   上下文的 —— 员工的 agent 读了本机文件、抓了内网页面，那些数据确实出了网。
--   停存正文后，「什么公司数据被发给了模型」在审计里只剩体量不剩内容。
--   `tool_skeleton` 保住的是「这次对话往模型里灌了 42KB 本地数据」这个信号。
--   MCP 调用不受影响：mcp_call_logs 单独记 capability、参数、状态、耗时。
--
-- 老数据不回填。`request_messages` 里确实有正文，但：
--   * 决策 #52 之前的行大多是**被切过 JSON 字符串**的半截数组（见
--     services/audit_content.py 模块注释），解析不出可靠的用户消息；
--   * 硬回填只会往 user_input 里填进一堆系统提示，比空着更误导。
--   列表页对 last_user_preview 为空的行会退回「取正文开头再拍平」。
--
-- 最后那句 DROP 是**不可逆的**：老行的正文就此消失。
--   开发库可以直接跑；真要留着老数据先备份，或者干脆先不跑最后一句 ——
--   ORM 不认识这一列，留着它也不影响写入（可空），只是白占空间。

ALTER TABLE `conversation_logs`
  ADD COLUMN `user_input` TEXT NULL
    COMMENT '本次用户输入全文（独占额度）'
    AFTER `model_name`,
  ADD COLUMN `system_prompt_sha256` VARCHAR(64) NULL
    COMMENT '系统提示词 sha256（只存指纹，不存全文）'
    AFTER `last_user_preview`,
  ADD COLUMN `system_prompt_chars` INT NOT NULL DEFAULT 0
    COMMENT '系统提示词字数'
    AFTER `system_prompt_sha256`,
  ADD COLUMN `tool_skeleton` TEXT NULL
    COMMENT '工具骨架 JSON [{"name","chars"}]，无工具时 NULL'
    AFTER `system_prompt_chars`;

-- ⚠️ 不可逆，确认过再跑
ALTER TABLE `conversation_logs` DROP COLUMN `request_messages`;
