import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.core.integration_access import confluence_principal
from app.core.security import Principal


@pytest.mark.asyncio
async def test_password_registrant_cannot_use_shared_confluence_credential(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_mode", "password", raising=False)
    principal = Principal("user-a", "local", frozenset(), {})
    with pytest.raises(HTTPException) as error:
        await confluence_principal(principal)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_password_admin_retains_shared_confluence_access(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_mode", "password", raising=False)
    principal = Principal("operator", "local", frozenset({settings.auth_admin_role}), {})
    assert await confluence_principal(principal) == principal
