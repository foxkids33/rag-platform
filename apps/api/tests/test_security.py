from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.routes.workspaces import WorkspaceCreate
from app.core.access import principal_can_access_workspace, workspace_for_principal
from app.core.config import Settings, settings
from app.core.security import OIDCVerifier, Principal
from app.db.models import Workspace
from app.main import app


class FakeJWKClient:
    def __init__(self, key) -> None:
        self.key = key

    def get_signing_key_from_jwt(self, _token: str):
        return SimpleNamespace(key=self.key)


class FakeSession:
    def __init__(self, workspace: Workspace | None) -> None:
        self.workspace = workspace

    async def scalar(self, _query):
        return self.workspace


def _principal(subject: str, tenant: str, *roles: str) -> Principal:
    return Principal(
        subject=subject,
        tenant_id=tenant,
        roles=frozenset(roles),
        claims={},
    )


def _workspace(subject: str = "user-a", tenant: str = "tenant-a") -> Workspace:
    return Workspace(
        tenant_id=tenant,
        user_id=subject,
        name="Workspace",
        base_knowledge_base_id=None,
        source_mode="USER_DOCUMENTS",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _auth_settings() -> Settings:
    return Settings(
        _env_file=None,
        auth_enabled=True,
        oidc_issuer="https://identity.example.com",
        oidc_audience="rag-api",
        oidc_jwks_url="https://identity.example.com/jwks",
        oidc_algorithm="RS256",
        oidc_tenant_claim="organization.id",
        oidc_roles_claim="realm_access.roles",
    )


def test_symmetric_oidc_algorithm_is_rejected() -> None:
    with pytest.raises(ValueError, match="asymmetric"):
        Settings(_env_file=None, oidc_algorithm="HS256")


def test_oidc_verifier_validates_identity_tenant_and_roles() -> None:
    config = _auth_settings()
    now = datetime.now(UTC)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(
        {
            "sub": "user-a",
            "organization": {"id": "tenant-a"},
            "realm_access": {"roles": ["rag-admin", "employee"]},
            "iss": config.oidc_issuer,
            "aud": config.oidc_audience,
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
    )

    principal = OIDCVerifier(config, FakeJWKClient(private_key.public_key())).verify(token)

    assert principal.subject == "user-a"
    assert principal.tenant_id == "tenant-a"
    assert principal.roles == frozenset({"rag-admin", "employee"})


def test_oidc_verifier_rejects_missing_tenant_claim() -> None:
    config = _auth_settings()
    now = datetime.now(UTC)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(
        {
            "sub": "user-a",
            "iss": config.oidc_issuer,
            "aud": config.oidc_audience,
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
    )

    with pytest.raises(ValueError, match="tenant claim"):
        OIDCVerifier(config, FakeJWKClient(private_key.public_key())).verify(token)


def test_workspace_access_is_scoped_to_owner_and_tenant() -> None:
    workspace = _workspace()

    assert principal_can_access_workspace(_principal("user-a", "tenant-a"), workspace)
    assert not principal_can_access_workspace(_principal("user-b", "tenant-a"), workspace)
    assert not principal_can_access_workspace(
        _principal("admin", "tenant-b", settings.auth_admin_role),
        workspace,
    )
    assert principal_can_access_workspace(
        _principal("admin", "tenant-a", settings.auth_admin_role),
        workspace,
    )


def test_workspace_owner_cannot_be_supplied_by_client() -> None:
    with pytest.raises(ValidationError, match="user_id"):
        WorkspaceCreate.model_validate({"name": "Workspace", "user_id": "attacker"})


def test_cross_tenant_workspace_lookup_returns_not_found() -> None:
    db = FakeSession(_workspace())
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            workspace_for_principal(
                db,
                db.workspace.id,
                _principal("admin", "tenant-b", settings.auth_admin_role),
            )
        )
    assert exc_info.value.status_code == 404


def test_local_auth_me_preserves_development_workflow() -> None:
    client = TestClient(app)
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 200
    assert response.json() == {
        "subject": settings.auth_local_subject,
        "tenant_id": settings.auth_local_tenant,
        "roles": [settings.auth_admin_role],
        "auth_enabled": False,
    }


def test_enabled_auth_requires_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True)
    client = TestClient(app)
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
