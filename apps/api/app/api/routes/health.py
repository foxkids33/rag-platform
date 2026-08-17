from fastapi import APIRouter, Depends

from app.core.config import settings
from app.core.security import Principal, get_current_principal

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": settings.app_name, "environment": settings.app_env}


@router.get("/system/info")
async def system_info(
    _principal: Principal = Depends(get_current_principal),
) -> dict:
    return {
        "service": settings.app_name,
        "environment": settings.app_env,
        "authentication": {
            "enabled": settings.auth_enabled,
            "mode": "oidc" if settings.auth_enabled else "local",
        },
        "embedding_model": settings.embedding_model,
        "embedding_dimension": settings.embedding_dim,
        "rerank_model": settings.rerank_model,
        "features": {
            "knowledge_bases": True,
            "workspace_overlay": True,
            "user_documents_only": True,
            "hybrid_retrieval": "planned",
            "knowledge_graph": "planned",
        },
    }
