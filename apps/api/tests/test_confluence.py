import asyncio

import httpx
import pytest
from pydantic import SecretStr

from app.api.routes import confluence as confluence_route
from app.core.config import settings
from app.core.security import Principal
from app.services.confluence import ConfluenceClient, ConfluenceError


def _configure_cloud(monkeypatch) -> None:
    monkeypatch.setattr(settings, "confluence_enabled", True)
    monkeypatch.setattr(settings, "confluence_deployment", "cloud")
    monkeypatch.setattr(settings, "confluence_base_url", "https://docs.example.test")
    monkeypatch.setattr(settings, "confluence_auth_mode", "basic")
    monkeypatch.setattr(settings, "confluence_email", "bot@example.test")
    monkeypatch.setattr(settings, "confluence_api_token", SecretStr("api-token"))


def test_cloud_page_uses_v2_storage_body_and_basic_auth(monkeypatch) -> None:
    _configure_cloud(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/wiki/api/v2/pages/12345"
        assert request.url.params["body-format"] == "storage"
        assert request.headers["Authorization"].startswith("Basic ")
        return httpx.Response(
            200,
            json={
                "id": "12345",
                "title": "Runbook",
                "body": {"storage": {"value": "<p>BLUE-KB-942</p>"}},
                "version": {"number": 7},
                "_links": {
                    "base": "https://docs.example.test/wiki",
                    "webui": "/spaces/ENG/pages/12345/Runbook",
                },
            },
        )

    client = ConfluenceClient(transport=httpx.MockTransport(handler))

    async def run():
        page = await client.get_page("12345")
        await client.close()
        return page

    page = asyncio.run(run())

    assert page.id == "12345"
    assert page.version == "7"
    assert page.storage_html == "<p>BLUE-KB-942</p>"
    assert page.source_url == "https://docs.example.test/wiki/spaces/ENG/pages/12345/Runbook"


def test_cloud_space_pagination_cannot_leave_configured_host(monkeypatch) -> None:
    _configure_cloud(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wiki/api/v2/spaces":
            return httpx.Response(200, json={"results": [{"id": "77", "key": "ENG"}]})
        return httpx.Response(
            200,
            headers={"Link": '<https://attacker.invalid/pages?cursor=x>; rel="next"'},
            json={"results": [{"id": "12345"}]},
        )

    client = ConfluenceClient(transport=httpx.MockTransport(handler))

    async def run():
        try:
            await client.list_space_page_ids("ENG")
        finally:
            await client.close()

    with pytest.raises(ConfluenceError, match="configured host"):
        asyncio.run(run())


def test_data_center_page_uses_content_api_and_bearer_token(monkeypatch) -> None:
    monkeypatch.setattr(settings, "confluence_enabled", True)
    monkeypatch.setattr(settings, "confluence_deployment", "data_center")
    monkeypatch.setattr(
        settings,
        "confluence_base_url",
        "https://confluence.example.test/confluence",
    )
    monkeypatch.setattr(settings, "confluence_auth_mode", "bearer")
    monkeypatch.setattr(settings, "confluence_access_token", SecretStr("personal-token"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/confluence/rest/api/content/ABC-1"
        assert request.url.params["expand"] == "body.storage,version,space,ancestors,metadata.labels"
        assert request.headers["Authorization"] == "Bearer personal-token"
        return httpx.Response(
            200,
            json={
                "id": "ABC-1",
                "title": "Runbook DC",
                "body": {"storage": {"value": "<p>Процедура восстановления</p>"}},
                "version": {"number": 11},
                "_links": {
                    "base": "https://confluence.example.test/confluence",
                    "webui": "/display/OPS/Runbook+DC",
                },
            },
        )

    client = ConfluenceClient(transport=httpx.MockTransport(handler))

    async def run():
        try:
            return await client.get_page("ABC-1")
        finally:
            await client.close()

    page = asyncio.run(run())

    assert page.version == "11"
    assert page.source_url == (
        "https://confluence.example.test/confluence/display/OPS/Runbook+DC"
    )


def test_regular_user_can_open_confluence_import_for_owned_workspace() -> None:
    principal = Principal(
        subject="regular-user",
        tenant_id="tenant-a",
        roles=frozenset(),
        claims={},
    )

    integration = asyncio.run(confluence_route.confluence_status(principal))

    assert integration.can_import is True
    assert integration.credentials_required is True


def test_personal_credentials_are_masked_and_override_service_account(monkeypatch) -> None:
    _configure_cloud(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Basic dXNlckBleGFtcGxlLnRlc3Q6dXNlci10b2tlbg=="
        return httpx.Response(200, json={"results": []})

    credentials = confluence_route.ConfluenceCredentials(
        email="user@example.test",
        token="user-token",
    )
    assert "user-token" not in repr(credentials)

    client = ConfluenceClient(
        email=credentials.email,
        token=credentials.token.get_secret_value(),
        transport=httpx.MockTransport(handler),
    )

    async def run() -> None:
        try:
            await client.check_connection()
        finally:
            await client.close()

    asyncio.run(run())
