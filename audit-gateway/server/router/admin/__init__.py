from fastapi import APIRouter
from router.admin.account import router as account_router
from router.admin.apikey import router as apikey_router
from router.admin.audit import router as audit_router
from router.admin.catalog import router as catalog_router
from router.admin.codex import router as codex_router
from router.admin.images import router as images_router
from router.admin.department import router as department_router
from router.admin.dingtalk import router as dingtalk_router
from router.admin.directory import router as directory_router
from router.admin.native_login import router as native_login_router
from router.admin.quota import router as quota_router
from router.admin.release import router as release_router
from router.admin.redaction import router as redaction_router
from router.admin.skill_hub import router as skill_hub_router
from router.admin.skill_report import router as skill_report_router
from router.admin.usage import router as usage_router
from router.admin.user import router as user_router
from router.admin.mcp import router as mcp_router
from router.admin.member import router as member_router
from router.admin.prompt_policy import router as prompt_policy_router

router = APIRouter(prefix="/admin", tags=["管理端接口"])

router.include_router(user_router)
router.include_router(dingtalk_router)
router.include_router(account_router)
router.include_router(member_router)
router.include_router(directory_router)
router.include_router(native_login_router)
router.include_router(apikey_router)
router.include_router(department_router)
router.include_router(catalog_router)
router.include_router(codex_router)
router.include_router(images_router)
router.include_router(quota_router)
router.include_router(usage_router)
router.include_router(audit_router)
router.include_router(mcp_router)
router.include_router(release_router)
router.include_router(skill_report_router)
router.include_router(skill_hub_router)
router.include_router(redaction_router)
router.include_router(prompt_policy_router)
