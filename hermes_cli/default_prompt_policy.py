"""Bundled offline baseline for the branded company prompt policy.

Operational endpoints, upload limits, and other fast-changing service metadata
do not belong here.  They are resolved from managed configuration or skills.
"""

from __future__ import annotations

from hermes_cli.default_soul import BRANDED_DEFAULT_SOUL_MD


BUNDLED_PROMPT_POLICY_VERSION = "bundled-2026-10-19.2"
BUNDLED_DEFAULT_PERSONA = BRANDED_DEFAULT_SOUL_MD

BUNDLED_COMPANY_CORE_POLICY = """示例组织规则：遵守用户授权，保护凭据和个人信息，执行重要变更前说明影响。组织管理员应通过管理后台配置自己的规则。"""
