from fastapi import APIRouter

from app.api.routes import answer, conversations, documents, health, knowledge_bases, search, workspaces

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(knowledge_bases.router)
api_router.include_router(workspaces.router)
api_router.include_router(documents.router)
api_router.include_router(conversations.router)
api_router.include_router(search.router)
api_router.include_router(answer.router)
