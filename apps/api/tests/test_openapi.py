from __future__ import annotations

import httpx
import pytest
from fastapi.routing import APIRoute

from app.api.router import api_router
from app.core.config import settings
from app.main import app


@pytest.fixture
def openapi_cache():
    previous = app.openapi_schema
    app.openapi_schema = None
    yield
    app.openapi_schema = previous


@pytest.mark.asyncio
async def test_documentation_is_served_through_api_prefix(openapi_cache) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        swagger = await client.get("/api/docs")
        specification = await client.get("/api/openapi.json")
        redoc = await client.get("/api/redoc")
        oauth_redirect = await client.get("/api/docs/oauth2-redirect")
        root = await client.get("/")
        old_docs = await client.get("/docs")
    assert swagger.status_code == 200
    assert "SwaggerUIBundle" in swagger.text
    assert "/api/openapi.json" in swagger.text
    assert "/api/docs/oauth2-redirect" in swagger.text
    assert specification.status_code == 200
    assert specification.headers["content-type"].startswith("application/json")
    assert specification.json()["openapi"].startswith("3.")
    assert redoc.status_code == 200 and "/api/openapi.json" in redoc.text
    assert oauth_redirect.status_code == 200
    assert root.json()["docs"] == "/api/docs"
    assert old_docs.status_code == 404


def _api_routes(router):
    for route in router.routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from _api_routes(route.original_router)


def test_openapi_covers_every_business_route(openapi_cache) -> None:
    # Current FastAPI may represent include_router lazily. Route schemas contain
    # their complete paths, so use those and recursively unpack router wrappers.
    expected = set()
    for route in _api_routes(api_router):
        if route.include_in_schema:
            for method in route.methods:
                path = route.path
                if not path.startswith("/api/v1"):
                    path = "/api/v1" + path
                expected.add((method.lower(), path))
    schema = app.openapi()
    documented = {
        (method, path)
        for path, operations in schema["paths"].items()
        if path.startswith("/api/v1/")
        for method in operations
        if method in {"get", "post", "put", "patch", "delete", "head", "options"}
    }
    assert expected, "Route traversal must not silently check an empty set"
    assert expected == documented
    operation_ids = [
        operation["operationId"]
        for operations in schema["paths"].values()
        for operation in operations.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]
    assert len(operation_ids) == len(set(operation_ids))


def test_stream_documents_sse_not_json(openapi_cache) -> None:
    route = app.openapi()["paths"]["/api/v1/workspaces/{workspace_id}/answer/stream"]["post"]
    assert set(route["responses"]["200"]["content"]) == {"text/event-stream"}
    assert route["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/AnswerRequest"
    }


@pytest.mark.parametrize("enabled", [False, True])
def test_user_auth_mode_does_not_remove_machine_security(
    monkeypatch, openapi_cache, enabled: bool
) -> None:
    monkeypatch.setattr(settings, "auth_enabled", enabled)
    schema = app.openapi()
    workspace = schema["paths"]["/api/v1/workspaces"]["get"]
    machine = schema["paths"]["/api/v1/connectors/{connector_id}/jobs/next"]["get"]
    if enabled:
        assert workspace["security"] == [{"HTTPBearer": []}]
    else:
        assert "security" not in workspace
    assert machine["security"] == [{"ConnectorBearer": []}]
