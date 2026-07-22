from fastapi import APIRouter

from app.api.routes import health, knowledge_bases, workspaces

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(knowledge_bases.router)
api_router.include_router(workspaces.router)
