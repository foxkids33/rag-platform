from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException, Request
from pydantic import SecretStr

from app.api.routes import confluence_personal as routes
from app.core.config import settings
from app.core.security import Principal, get_current_principal
from app.db.session import get_db
from app.main import app
from app.services import confluence_personal as personal


@pytest.fixture
def configured(tmp_path, monkeypatch):
    key_file = tmp_path / "key"
    key_file.write_bytes(Fernet.generate_key())
    for field, value in {
        "auth_enabled": True,
        "auth_mode": "password",
        "auth_cookie_secure": True,
        "confluence_personal_key_file": str(key_file),
        "confluence_personal_socket": "/unused",
        "confluence_base_url": "https://wiki.test",
        "confluence_auth_mode": "bearer",
        "confluence_deployment": "data_center",
        "confluence_access_token": SecretStr("server-account"),
    }.items():
        monkeypatch.setattr(settings, field, value)
    storage = {}
    expiry = {}

    async def put(key, value, *, ex):
        storage[key] = value.decode()
        expiry[key] = ex

    redis = SimpleNamespace(
        set=put,
        get=AsyncMock(side_effect=storage.get),
        delete=AsyncMock(side_effect=lambda key: storage.pop(key, None)),
    )
    monkeypatch.setattr(personal, "redis", redis)
    return storage, expiry


async def test_session_bound_upstream_permissions_and_vpn_outage(configured, monkeypatch):
    storage, expiry = configured
    observed = []
    principals = {
        "alice": Principal("alice", "tenant", frozenset(), {}),
        "bob": Principal("bob", "tenant", frozenset(), {}),
        "admin": Principal("admin", "tenant", frozenset({settings.auth_admin_role}), {}),
    }
    owner = uuid4()
    credentials = {"token-A": "SPACE-A", "token-B": "SPACE-B"}
    vpn_up = True

    async def current(request: Request):
        request.state.auth_session_hash = request.headers.get("session", "login1")
        request.state.auth_session_expires_at = datetime.now(UTC) + timedelta(hours=1)
        return principals[request.headers["user"]]

    async def workspace(db, workspace_id, principal):
        if workspace_id != owner and principal.subject != "admin":
            raise HTTPException(404, "Workspace not found")
        return SimpleNamespace(
            user_id="someone" if principal.subject == "admin" else principal.subject
        )

    def wiki(request):
        token = request.headers["Authorization"].removeprefix("Bearer ")
        observed.append(token)
        if not vpn_up:
            raise httpx.ConnectError("Offline", request=request)
        if token not in credentials:
            return httpx.Response(401)
        if request.url.path == "/rest/api/user/current":
            return httpx.Response(200, json={"username": token[-1], "type": "known"})
        assert request.url.path == "/rest/api/space"
        return httpx.Response(200, json={"results": [{"key": credentials[token], "name": "Team"}]})

    original = personal.PersonalConfluenceClient
    monkeypatch.setattr(
        personal,
        "PersonalConfluenceClient",
        lambda token, spaces: original(token, spaces, transport=httpx.MockTransport(wiki)),
    )
    monkeypatch.setattr(routes, "workspace_for_principal", workspace)
    async def unused_db():
        return object()

    previous = dict(app.dependency_overrides)
    app.dependency_overrides.update({get_current_principal: current, get_db: unused_db})
    connection = "/api/v1" + routes.CONNECTION
    spaces = "/api/v1" + routes.WORKSPACE.format(workspace_id=owner) + "/spaces"

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://test"
        ) as client:
            alice = {"user": "alice"}
            bob = {"user": "bob"}
            assert not (await client.get(connection, headers=alice)).json()["ready"]
            for headers, token in [(alice, "token-A"), (bob, "token-B")]:
                response = await client.post(connection, headers=headers, json={"token": token})
                assert response.status_code == 200 and response.json()["ready"]
                assert token not in response.text
                response = await client.post(spaces, headers=headers)
                assert [s["key"] for s in response.json()["spaces"]] == [credentials[token]]
            assert len(storage) == 2 and all(0 < ttl <= 3600 for ttl in expiry.values())
            assert all("token-A" not in item and "token-B" not in item for item in storage.values())

            count = len(observed)
            assert (
                await client.post(spaces, headers={**alice, "session": "other"})
            ).status_code == 409
            assert (await client.post(spaces, headers={"user": "admin"})).status_code == 404
            assert len(observed) == count

            rejected = await client.post(
                connection, headers=alice, json={"token": "expired-secret"}
            )
            assert rejected.status_code == 403 and "expired-secret" not in rejected.text
            vpn_up = False
            unavailable = await client.post(spaces, headers=alice)
            assert unavailable.status_code == 502 and "token-A" not in unavailable.text
            vpn_up = True
            assert (await client.post(spaces, headers=alice)).json()["spaces"][0][
                "key"
            ] == "SPACE-A"
            assert (await client.delete(connection, headers=alice)).status_code == 204
            assert (await client.post(spaces, headers=alice)).status_code == 409
            assert (await client.post(spaces, headers=bob)).status_code == 200
            assert "server-account" not in observed
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


async def test_personal_token_ciphertext_cannot_be_moved_between_tenants(configured):
    storage, _ = configured
    request = Request({"type": "http", "headers": []})
    request.state.auth_session_hash = "same-session"
    alice = Principal("a", "tenant1", frozenset(), {})
    other = Principal("a", "tenant2", frozenset(), {})
    a, b = (personal.session_key(request, user) for user in (alice, other))
    await personal.save_token(a, "dummy-private-token", datetime.now(UTC) + timedelta(minutes=1))
    storage[b] = storage[a]
    assert a != b and await personal.get_token(b) is None
