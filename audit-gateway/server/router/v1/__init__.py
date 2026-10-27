"""OpenAI 兼容的转发接口

桌面客户端把网关地址当 base_url 直接用，所以路径必须与 OpenAI 一致：
    POST /v1/chat/completions
    GET  /v1/models
"""

from fastapi import APIRouter
from router.v1.chat import router as chat_router
from router.v1.client_update import router as client_update_router
from router.v1.directory import router as directory_router
from router.v1.mcp import router as mcp_router
from router.v1.images import router as images_router
from router.v1.project_turns import router as project_turns_router
from router.v1.projects import router as projects_router
from router.v1.prompt_policy import router as prompt_policy_router
from router.v1.redaction_policy import router as redaction_policy_router
from router.v1.skill_hub import router as skill_hub_router
from router.v1.skill_reports import router as skill_reports_router
from router.v1.mcp_credentials import router as mcp_credentials_router

router = APIRouter(prefix="/v1", tags=["模型转发"])

router.include_router(chat_router)
router.include_router(mcp_router)
router.include_router(images_router)
router.include_router(redaction_policy_router)
router.include_router(prompt_policy_router)
router.include_router(client_update_router)
router.include_router(skill_reports_router)
router.include_router(skill_hub_router)
router.include_router(projects_router)
# 全员通讯录（2026-09-07）。客户端把人拉进项目要先找得到人，而后台的账号列表
# 要 user:view —— 普通员工没有。开放范围与钉钉一致：全员可见，字段只有四个。
router.include_router(directory_router)
# 群聊 agent 的执行租约（阶段 27-E）。单独一个文件：projects.py 已经够长，
# 而这一组的读者需要的是租约与熔断那一套背景，不是成员与任务那一套。
router.include_router(project_turns_router)
router.include_router(mcp_credentials_router)
