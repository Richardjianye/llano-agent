-- 桌面端自有更新：版本清单表（阶段 5 · U1）
--
-- 执行方式：
--   mysql -u root -p audit_gateway < scripts/migrations/2026-08-24-client-releases.sql
--
-- 这张表是客户端更新的唯一来源。客户端不再访问 Hermes GitHub、Nous 官方源
-- 或 backend git update（旁路封堵见阶段 5 · U5）。
--
-- ⚠️ manifest_json 是唯一真源，其余业务列是它的冗余副本
--
--   签名覆盖的是清单的字节。按列存、读时再拼回一个 dict 的话，任何一处格式
--   漂移都会让重建出来的清单与签名对不上 —— artifact_size 从 int 变成
--   字符串、released_at 被 DATETIME 列重新格式化成 '2026-08-24 02:30:00'，
--   都够了。表现是**全量客户端更新失败**，而库里每个字段看上去都对。
--   存原文就没有这个问题；其余列只用于后台筛选和展示，不参与验签。
--
--   released_at 因此是 VARCHAR 而不是 DATETIME，且是全库唯一的 UTC 时间戳
--   （其余走服务器本地时间，见 model/database.now）。写它的是离线签名机，
--   那台机器的时区不受我们控制。
--
-- ⚠️ 网关上没有私钥
--
--   signature 只能由离线签名机产生（audit-gateway/release/sign_release.py）。
--   发布接口用公钥验一遍才准入库：网关验得了，签不出来。私钥放上来的话，
--   网关一旦被拿下，攻击者就能签出客户端照单全收的恶意更新，而签名的全部
--   意义就是「即使分发通道被控制，客户端也能识别出这不是我们发的」。
--
-- 唯一键 (version, platform, arch)：同一版本对同一平台架构只能有一条，
--   否则「哪一条才算数」没有答案。要改已发布的内容就下架旧条目再发新版本。

CREATE TABLE IF NOT EXISTS `client_releases` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,

  `manifest_json` TEXT NOT NULL COMMENT '规范化清单原文，签名覆盖的就是它',
  `signature` TEXT NOT NULL COMMENT '离线签名机产出的 Ed25519 签名（base64）',
  `public_key_fp` VARCHAR(32) NOT NULL COMMENT '签名所用公钥指纹，便于确认信任根一致',

  `version` VARCHAR(32) NOT NULL,
  `platform` VARCHAR(16) NOT NULL COMMENT 'darwin/win32，与 Electron process.platform 同名',
  `arch` VARCHAR(16) NOT NULL COMMENT 'x64/arm64',
  `policy` VARCHAR(16) NOT NULL COMMENT 'optional/required',
  `min_supported_version` VARCHAR(32) NOT NULL COMMENT '低于此版本的客户端必须强制更新',
  `artifact_url` VARCHAR(512) NOT NULL,
  `artifact_sha256` VARCHAR(64) NOT NULL,
  `artifact_size` BIGINT NOT NULL,
  `released_at` VARCHAR(24) NOT NULL COMMENT 'UTC ISO，进签名，不可改格式',
  `release_notes` TEXT NULL,

  `status` INT NOT NULL DEFAULT 1 COMMENT '1 上架 / 0 下架',
  `published_by` VARCHAR(36) NOT NULL COMMENT '发布人 uid',
  `created_at` DATETIME NOT NULL,
  `updated_at` DATETIME NOT NULL,

  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_client_release_target` (`version`, `platform`, `arch`),
  KEY `ix_client_releases_lookup` (`platform`, `arch`, `status`),
  KEY `ix_client_releases_version` (`version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='桌面端版本清单：客户端更新的唯一来源';
