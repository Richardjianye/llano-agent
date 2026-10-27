-- 项目先落本地库，Gitea 仓库异步、幂等创建。旧项目默认 pending，部署后会补建。

ALTER TABLE `projects`
  ADD COLUMN `git_repo_id` BIGINT NULL COMMENT 'Gitea 仓库 ID' AFTER `git_repo_url`,
  ADD COLUMN `git_repo_name` VARCHAR(128) NULL COMMENT '稳定的 Gitea 仓库名' AFTER `git_repo_id`,
  ADD COLUMN `git_repo_status` VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'pending / provisioning / failed / ready' AFTER `git_repo_name`,
  ADD COLUMN `git_repo_error` VARCHAR(512) NULL COMMENT '最近一次建仓失败摘要' AFTER `git_repo_status`,
  ADD COLUMN `git_repo_attempted_at` DATETIME NULL COMMENT '最近一次建仓尝试时间' AFTER `git_repo_error`,
  ADD INDEX `ix_projects_git_repo_status` (`git_repo_status`);
