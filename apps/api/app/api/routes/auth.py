from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.config import settings
from app.core.security import Principal, get_current_principal

router = APIRouter(prefix="/auth", tags=["authentication"])


class PrincipalOut(BaseModel):
    subject: str
    tenant_id: str
    roles: list[str]
    auth_enabled: bool


@router.get("/me", response_model=PrincipalOut)
async def current_user(
    principal: Principal = Depends(get_current_principal),
) -> PrincipalOut:
    return PrincipalOut(
        subject=principal.subject,
        tenant_id=principal.tenant_id,
        roles=sorted(principal.roles),
        auth_enabled=settings.auth_enabled,
    )
