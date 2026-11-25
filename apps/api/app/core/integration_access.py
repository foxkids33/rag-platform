"""Do not share the operator's Confluence credential with new registrants."""

from fastapi import Depends

from app.core.config import settings
from app.core.security import Principal, get_current_principal, require_tenant_admin


async def confluence_principal(
    principal: Principal = Depends(get_current_principal),
) -> Principal:
    # Password accounts start with private data only. Confluence uses a shared
    # operator credential, not the registrant's individual Confluence ACL.
    # Preserve existing tenant policies in explicitly configured OIDC installs.
    if settings.auth_enabled and getattr(settings, "auth_mode", "oidc") == "password":
        require_tenant_admin(principal)
    return principal
