"""Direct access contracts, with no external Confluence, tokens or database."""

import uuid

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import SecretStr, ValidationError

from app.api.routes import connectors as routes
from app.core.config import Settings, settings
from app.core.security import Principal, get_current_principal
from app.db.session import get_db
from app.services.confluence import ConfluenceError, ConfluencePage
from app.services.confluence_direct import DirectConfluenceClient, selected_confluence_mode


@pytest.fixture
def configured(monkeypatch):
    values = {
        "confluence_connection_mode": "direct",
        "confluence_enabled": True,
        "confluence_connector_enabled": False,
        "confluence_deployment": "data_center",
        "confluence_base_url": "https://docs.example.test/confluence",
        "confluence_auth_mode": "bearer",
        "confluence_access_token": SecretStr("test-service-token"),
        "confluence_allowed_spaces": "ENG,OPS",
    }
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)


@pytest.fixture
def principal():
    return Principal(subject="alice", tenant_id="tenant-a", roles=frozenset(), claims={})


def page_payload(page_id="42", space="ENG"):
    return {
        "id": page_id,
        "title": "Recovery guide",
        "space": {"key": space},
        "version": {"number": 3},
        "body": {"storage": {"value": "<p>Restore safely.</p>"}},
        "_links": {"webui": "/display/ENG/Recovery"},
    }


async def read_with(client, method, *args):
    try:
        return await getattr(client, method)(*args)
    finally:
        await client.close()


def test_auto_keeps_existing_connector_and_explicit_mode_wins(configured, monkeypatch):
    monkeypatch.setattr(settings, "confluence_connection_mode", "auto")
    monkeypatch.setattr(settings, "confluence_connector_enabled", True)
    assert selected_confluence_mode() == "connector"
    monkeypatch.setattr(settings, "confluence_connection_mode", "direct")
    assert selected_confluence_mode() == "direct"
    monkeypatch.setattr(settings, "confluence_connection_mode", "auto")
    monkeypatch.setattr(settings, "confluence_connector_enabled", False)
    assert selected_confluence_mode() == "direct"


def test_allowlist_config_does_not_accept_paths_or_wildcards():
    for value in ("*", "../OPS", "ENG,https://internal/", "~"):
        with pytest.raises(ValidationError, match="CONFLUENCE_ALLOWED_SPACES"):
            Settings(_env_file=None, confluence_allowed_spaces=value)
    config = Settings(_env_file=None, confluence_allowed_spaces=" ENG,OPS,ENG,~alice ")
    assert config.confluence_allowed_space_keys == ["ENG", "OPS", "~alice"]


async def test_connection_status_returns_configuration_not_secrets_or_network_health(
    configured,
    monkeypatch,
    principal,
):
    class NoDatabase:
        def __getattr__(self, name):
            raise AssertionError(f"Direct configuration status must not query DB: {name}")

    result = await routes.confluence_connection_status(NoDatabase(), principal)
    assert result.mode == "direct"
    assert result.ready and result.configured
    assert result.host == "docs.example.test"
    assert result.allowed_spaces == ["ENG", "OPS"]
    assert result.missing_configuration == []
    assert result.connector is None
    assert "test-service-token" not in result.model_dump_json()
    assert "Authorization" not in result.model_dump_json()
    monkeypatch.setattr(settings, "confluence_access_token", SecretStr(""))
    monkeypatch.setattr(settings, "confluence_allowed_spaces", "")
    result = await routes.confluence_connection_status(NoDatabase(), principal)
    assert not result.ready
    assert set(result.missing_configuration) == {
        "CONFLUENCE_ACCESS_TOKEN",
        "CONFLUENCE_ALLOWED_SPACES",
    }


async def test_data_center_context_path_and_server_bearer_auth(configured):
    def handler(request):
        assert request.url.path == "/confluence/rest/api/content/42"
        assert request.headers["Authorization"] == "Bearer test-service-token"
        assert "space" in request.url.params["expand"]
        return httpx.Response(200, json=page_payload())

    client = DirectConfluenceClient(transport=httpx.MockTransport(handler))
    page = await read_with(client, "get_page", "42")
    assert page.space_key == "ENG"
    assert page.version == "3"


@pytest.mark.parametrize("space", ["HR", "", None])
async def test_page_body_cannot_be_imported_from_outside_allowlist(configured, space):
    client = DirectConfluenceClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=page_payload(space=space)),
        )
    )
    with pytest.raises(ConfluenceError, match="allowlist"):
        await read_with(client, "get_page", "42")


async def test_space_listing_fetches_only_allowlisted_spaces(configured):
    requested = []

    def handler(request):
        requested.append(request.url.path)
        key = request.url.path.rsplit("/", 1)[1]
        return httpx.Response(200, json={"key": key, "name": key, "homepage": {"id": "42"}})

    client = DirectConfluenceClient(transport=httpx.MockTransport(handler))
    result = await read_with(client, "list_spaces")
    assert [item["key"] for item in result] == ["ENG", "OPS"]
    assert requested == ["/confluence/rest/api/space/ENG", "/confluence/rest/api/space/OPS"]


async def test_parent_space_checked_before_loading_its_children(configured):
    requested = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(200, json=page_payload(space="HR"))

    client = DirectConfluenceClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ConfluenceError, match="allowlist"):
        await read_with(client, "list_children", "ENG", "42")
    assert requested == ["/confluence/rest/api/content/42"]


@pytest.mark.parametrize(
    "next_link",
    [
        "https://attacker.invalid/steal",
        "/confluence/rest/api/content?start=0",
    ],
)
async def test_space_pagination_rejects_off_host_or_stalled_next_links(configured, next_link):
    client = DirectConfluenceClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "results": [page_payload()],
                    "_links": {"next": next_link},
                },
            ),
        )
    )
    with pytest.raises(ConfluenceError, match="configured host|did not advance"):
        await read_with(client, "list_space_pages", "ENG", 0)


async def test_cloud_v2_resolves_actual_space_id_before_accepting_page(configured, monkeypatch):
    monkeypatch.setattr(settings, "confluence_deployment", "cloud")
    monkeypatch.setattr(settings, "confluence_base_url", "https://docs.example.test/wiki")
    requested = []

    def handler(request):
        requested.append(request.url.path)
        if request.url.path == "/wiki/api/v2/pages/42":
            assert request.url.params["body-format"] == "storage"
            return httpx.Response(200, json={**page_payload(), "space": None, "spaceId": "99"})
        assert request.url.path == "/wiki/api/v2/spaces/99"
        return httpx.Response(200, json={"id": "99", "key": "HR", "name": "Restricted"})

    client = DirectConfluenceClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ConfluenceError, match="allowlist"):
        await read_with(client, "get_page", "42")
    assert requested == ["/wiki/api/v2/pages/42", "/wiki/api/v2/spaces/99"]


async def test_cloud_cursor_pagination_and_page_space_validation(configured, monkeypatch):
    monkeypatch.setattr(settings, "confluence_deployment", "cloud")
    monkeypatch.setattr(settings, "confluence_base_url", "https://docs.example.test")

    def handler(request):
        if request.url.path == "/wiki/api/v2/spaces":
            assert request.url.params["keys"] == "ENG"
            return httpx.Response(
                200, json={"results": [{"id": "7", "key": "ENG", "name": "Engineering"}]}
            )
        assert request.url.path == "/wiki/api/v2/spaces/7/pages"
        cursor = request.url.params.get("cursor")
        assert cursor in {None, "cursor-2"}
        page = {**page_payload("43" if cursor else "42"), "spaceId": "7"}
        payload = {"results": [page]}
        if cursor is None:
            payload["_links"] = {"next": "/wiki/api/v2/spaces/7/pages?cursor=cursor-2"}
        return httpx.Response(200, json=payload)

    client = DirectConfluenceClient(transport=httpx.MockTransport(handler))
    try:
        first = await client.list_space_pages("ENG")
        second = await client.list_space_pages("ENG", first["next_start"], first["next_cursor"])
    finally:
        await client.close()
    assert first["next_start"] == 1 and first["next_cursor"] == "cursor-2"
    assert second["next_start"] is None and second["next_cursor"] is None
    assert [first["pages"][0]["id"], second["pages"][0]["id"]] == ["42", "43"]


async def test_workspace_permission_is_checked_before_any_upstream_access(
    configured,
    monkeypatch,
    principal,
):
    async def deny(*args):
        raise HTTPException(status_code=404, detail="Workspace not found")

    def no_client():
        raise AssertionError("Unauthorised request must not create an upstream client")

    monkeypatch.setattr(routes, "workspace_for_principal", deny)
    monkeypatch.setattr(routes, "DirectConfluenceClient", no_client)
    with pytest.raises(HTTPException) as error:
        await routes.list_direct_spaces(uuid.uuid4(), object(), principal)
    assert error.value.status_code == 404


@pytest.mark.parametrize(
    "actual_space,actual_version,expected_failures",
    [
        ("HR", "3", 1),
        ("OPS", "3", 1),
        ("ENG", "4", 1),
        ("ENG", "3", 0),
    ],
)
async def test_import_enforces_actual_space_and_version_not_browser_claim(
    configured,
    monkeypatch,
    principal,
    actual_space,
    actual_version,
    expected_failures,
):
    imported = []

    async def allow(*args):
        return None

    async def fetch_page(self, page_id):
        # Deliberately bypass client validation to check the API's boundary too.
        return ConfluencePage(
            id=page_id,
            title="Guide",
            storage_html="body",
            version=actual_version,
            source_url=None,
            space_key=actual_space,
        )

    async def save(db, workspace_id, page, *, force, principal):
        assert principal is None
        imported.append(page.id)
        return routes.ConfluenceImportItem(
            page_id=page.id, page_version=page.version, title=page.title, status="created"
        )

    monkeypatch.setattr(routes, "workspace_for_principal", allow)
    monkeypatch.setattr(DirectConfluenceClient, "get_page", fetch_page)
    monkeypatch.setattr(routes, "_import_page", save)
    result = await routes.import_direct_pages(
        uuid.uuid4(),
        routes.DirectImportRequest(
            page_ids=["42"], page_spaces={"42": "ENG"}, expected_versions={"42": "3"}
        ),
        object(),
        principal,
    )
    assert result.failed == expected_failures
    assert result.queued == 1 - expected_failures
    assert imported == ([] if expected_failures else ["42"])


async def test_direct_routes_keep_authentication_dependency(configured):
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")

    async def deny():
        raise HTTPException(status_code=401, detail="Authentication required")

    async def unused_db():
        yield object()

    app.dependency_overrides[get_current_principal] = deny
    app.dependency_overrides[get_db] = unused_db
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/integrations/confluence/connection")
        assert response.status_code == 401
        response = await client.post(
            f"/api/v1/workspaces/{uuid.uuid4()}/integrations/confluence/direct/spaces"
        )
        assert response.status_code == 401
