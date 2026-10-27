-- 群聊会话令牌（阶段 24）
--
-- 阶段 12 落下了「作用域只能收紧」这条规则，但凭据侧恒为 private —— 那半刀
-- 等的就是这两列。在此之前「这一轮是群聊」只能由客户端发头声明，忘发或被
-- 控制的客户端不发就静默降级成 private。
--
-- 存量行全部是个人设备 Key，落 private —— 与加列前的行为逐字一致。
ALTER TABLE `api_keys`
  ADD COLUMN `scope` VARCHAR(16) NOT NULL DEFAULT 'private'
    COMMENT 'private=个人设备Key group=群聊会话令牌',
  ADD COLUMN `scope_project_id` INT NULL
    COMMENT '群聊令牌绑定的项目ID；private 恒为空';

ALTER TABLE `api_keys`
  ADD INDEX `ix_api_keys_scope_project` (`scope_project_id`);
