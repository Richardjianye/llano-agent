-- 技能库分层：部门空间 / 公司空间（阶段 21）
--
-- 负责人 2026-08-31 定：
--   - 客户端发起「发布到**部门**空间」的请求，**不上传已安装技能列表**
--     （那会让「员工装了哪些技能」变成网关上的数据，没有非做不可的理由）
--   - 主管在后台筛选并发布到**公司**空间
--   - **取消管理部门审核**：管理部门基本不上线看，而且已经过主管审查。出问题由主管
--     撤回、或管理部门登录后台下架。多一道没人处理的审批队列，等于把技能永远卡在
--     部门里
--
-- 所以服务端只有两级，**没有 personal**：个人技能留在客户端本地。
--
-- ⚠️ scope_dept_id 与 owner_dept_id 是两件事
--
--   owner_dept_id 是「首发人当时在哪个部门」的快照，用于技能指标，不随晋级变。
--   scope_dept_id 是「这个技能现在属于哪个部门空间」，主管可以晋级或退回。
--   两者会分叉，合成一个字段的话，技能一晋级到公司空间，原部门的指标就少一个。

ALTER TABLE `skill_hub_skills`
  ADD COLUMN `scope` VARCHAR(16) NOT NULL DEFAULT 'dept'
    COMMENT 'dept=部门空间 / company=公司空间' AFTER `description`,
  ADD COLUMN `scope_dept_id` INT NULL
    COMMENT '所属部门空间；company 时为空' AFTER `scope`,
  ADD COLUMN `promoted_by_uid` VARCHAR(36) NULL COMMENT '把它发到公司空间的人',
  ADD COLUMN `promoted_at` DATETIME NULL COMMENT '晋级时间',
  ADD KEY `ix_skill_hub_scope` (`scope`, `scope_dept_id`, `status`);

-- 存量数据落到**部门**空间，不是公司空间。
--
-- 分层之前所有技能都是全公司可见的，所以「保持现状」= 全落 company。但那是错的：
-- 分层的目的就是让技能先在部门里沉淀，一次性把存量全部提到公司空间，等于把这个
-- 机制在第一天就架空了。落到发布者当时的部门，符合「它本来就是那个部门的人写的」。
--
-- 没有部门的（历史脏数据）留在 NULL：那种行谁都看不到，会被人发现并处理 ——
-- 比悄悄归到某个部门好。
UPDATE `skill_hub_skills` SET `scope` = 'dept', `scope_dept_id` = `owner_dept_id`
  WHERE `scope_dept_id` IS NULL;
