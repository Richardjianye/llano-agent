from fastapi import APIRouter
from router.client.model import router as model_router

router = APIRouter(prefix="/client", tags=["客户端接口"])

router.include_router(model_router)
