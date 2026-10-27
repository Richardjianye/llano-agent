-- 会话审计按 Hermes 会话 ID 串联（决策 #52 · 第一段）
--
-- 执行方式：
--   mysql -u root -p audit_gateway < scripts/migrations/2026-08-23-audit-session-linking.sql
--
-- 要解决的问题：一次提问在审计里散成十几条独立记录 —— 标题生成 1 条、
--   主对话 N 个 agent 轮次、命令审批扫描若干条 —— 彼此看不出关系。
--   Hermes 自己就给每次会话分配了 ID（形如 20260821_151706_ac236f，
--   见 agent/agent_init.py），客户端在私有审计信封里带过来，这里存下来。
--
-- ⚠️ session_id 是**客户端填的，可以伪造**。三条硬规矩（决策 #52）：
--   1. 只用于把记录串起来看，**绝不参与鉴权**
--   2. 串联**限定在同一 user_uid 内** —— 否则填别人的 session_id
--      就能把自己的记录挂到别人的会话下面。复合索引把 user_uid 放在第一列，
--      就是在索引层面复述这条规矩
--   3. 入库前按 ^[A-Za-z0-9_-]{1,64}$ 校验，不合规存 NULL
--      （见 services/audit_envelope.py）
--
-- 老数据不回填：改动之前的记录本来就没有会话 ID，回填等于凭空编造关系。
--   session_id 为 NULL 的行在列表页退化成「一行一条」照常显示 —— 这条不是
--   将就，是要求：stock Hermes 和绕过客户端的直连调用永远没有会话 ID，
--   「分不了组就不显示」正好把最该看的那类调用藏起来。
--
-- turn_kind 默认 'chat' 而不是 'other'：other 那一档在列表页默认隐藏，
--   老数据和没带信封的调用都落进默认值，必须落在「默认可见」那一档。

ALTER TABLE `conversation_logs`
  ADD COLUMN `session_id` VARCHAR(64) NULL
    COMMENT 'Hermes 会话ID（客户端给，不可信，仅用于串联展示）'
    AFTER `user_uid`,
  ADD COLUMN `turn_kind` VARCHAR(32) NOT NULL DEFAULT 'chat'
    COMMENT '轮次类型：chat/title/approval/other'
    AFTER `session_id`,
  ADD INDEX `ix_conversation_logs_session_id` (`session_id`),
  ADD INDEX `ix_conversation_logs_user_session` (`user_uid`, `session_id`);
