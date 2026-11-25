import asyncio

import httpx
import pytest

from confluence_connector.config import Settings
from confluence_connector.confluence import ConfluenceClient, ConfluenceError
from confluence_connector.runner import execute_job


def _settings(**overrides) -> Settings:
    values = {
        "rag_url": "https://rag.example.test",
        "confluence_url": "https://confluence.example.test",
        "confluence_auth_mode": "bearer",
        "confluence_token": "test-token",
        "confluence_allowed_spaces": "KBR",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_list_spaces_filters_against_local_allowlist() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/api/space"
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"key": "KBR", "name": "Knowledge Base", "homepage": {"id": "10"}},
                    {"key": "HR", "name": "Human Resources", "homepage": {"id": "20"}},
                ]
            },
        )

    client = ConfluenceClient(_settings(), transport=httpx.MockTransport(handler))

    async def run():
        try:
            return await client.list_spaces()
        finally:
            await client.close()

    assert asyncio.run(run()) == [
        {
            "key": "KBR",
            "name": "Knowledge Base",
            "homepage_id": "10",
            "web_url": None,
        }
    ]


def test_whole_space_listing_uses_content_pagination_not_homepage():
    starts = []

    def handler(request):
        assert request.url.path == "/rest/api/content"
        assert request.url.params["spaceKey"] == "KBR"
        assert request.url.params["type"] == "page"
        assert request.url.params["status"] == "current"
        start = int(request.url.params["start"])
        starts.append(start)
        return httpx.Response(200, json={
            "results": [{"id": str(start + 10), "title": "Page outside homepage tree",
                         "space": {"key": "KBR"}, "version": {"number": 1}}],
            "_links": {"next": "/rest/api/content?spaceKey=KBR&start=1"} if start == 0 else {},
        })

    async def run():
        client = ConfluenceClient(_settings(), transport=httpx.MockTransport(handler))
        try:
            first = await execute_job(_settings(), client, {
                "type": "LIST_SPACE_PAGES", "payload": {"space_key": "KBR", "start": 0},
            })
            second = await client.list_space_pages("KBR", first["next_start"])
            assert second["next_start"] is None
            assert [first["pages"][0]["id"], second["pages"][0]["id"]] == ["10", "11"]
            with pytest.raises(ConfluenceError, match="allowlist"):
                await client.list_space_pages("HR")
        finally:
            await client.close()
    asyncio.run(run())
    assert starts == [0, 1]


def test_whole_space_listing_rejects_nonadvancing_pagination():
    async def run():
        client = ConfluenceClient(
            _settings(), transport=httpx.MockTransport(lambda _: httpx.Response(
            200, json={"results": [{"id": "10", "title": "Page", "space": {"key": "KBR"}}],
                       "_links": {"next": "/rest/api/content?start=0"}},
        )))
        try:
            with pytest.raises(ConfluenceError, match="did not advance"):
                await client.list_space_pages("KBR")
        finally:
            await client.close()
    asyncio.run(run())


def test_root_and_child_tree_are_loaded_lazily() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/api/space/KBR":
            return httpx.Response(200, json={"homepage": {"id": "10"}})
        if request.url.path == "/rest/api/content/10":
            return httpx.Response(
                200,
                json={
                    "id": "10",
                    "title": "KBR Home",
                    "space": {"key": "KBR"},
                    "version": {"number": 3},
                    "children": {"page": {"size": 1}},
                },
            )
        if request.url.path == "/rest/api/content/10/child/page":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "202211666",
                            "title": "Test page",
                            "space": {"key": "KBR"},
                            "version": {"number": 7},
                            "children": {"page": {"size": 0}},
                        }
                    ]
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    client = ConfluenceClient(_settings(), transport=httpx.MockTransport(handler))

    async def run():
        try:
            root = await client.list_children("KBR", None)
            children = await client.list_children("KBR", "10")
            return root, children
        finally:
            await client.close()

    root, children = asyncio.run(run())
    assert root[0]["id"] == "10"
    assert root[0]["has_children"] is True
    assert children[0]["id"] == "202211666"
    assert children[0]["parent_id"] == "10"


def test_connector_rejects_unknown_operations_and_disallowed_spaces() -> None:
    client = ConfluenceClient(_settings(), transport=httpx.MockTransport(lambda _: None))

    async def run():
        try:
            with pytest.raises(ValueError, match="unsupported"):
                await execute_job(_settings(), client, {"type": "FETCH_URL", "payload": {}})
            with pytest.raises(ValueError, match="allowlist"):
                await execute_job(
                    _settings(),
                    client,
                    {
                        "type": "LIST_CHILDREN",
                        "payload": {"space_key": "HR", "parent_id": None},
                    },
                )
        finally:
            await client.close()

    asyncio.run(run())


def test_confluence_pagination_cannot_leave_configured_host() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Link": '<https://attacker.invalid/next>; rel="next"'},
            json={"results": []},
        )

    client = ConfluenceClient(_settings(), transport=httpx.MockTransport(handler))

    async def run():
        try:
            await client.list_spaces()
        finally:
            await client.close()

    with pytest.raises(ConfluenceError, match="configured host"):
        asyncio.run(run())


@pytest.mark.parametrize("status", [429, 503])
def test_transient_read_failure_is_retried_without_losing_page(status):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(status)
        return httpx.Response(
            200,
            json={
                "id": "42",
                "title": "Page",
                "space": {"key": "KBR"},
                "version": {"number": 1},
                "body": {"storage": {"value": "<p>ok</p>"}},
            },
        )

    client = ConfluenceClient(
        _settings(confluence_retry_delay_seconds=0),
        transport=httpx.MockTransport(handler),
    )

    async def run():
        try:
            return await client.get_page("42", "KBR")
        finally:
            await client.close()

    assert asyncio.run(run())["id"] == "42"
    assert len(calls) == 3


def test_permission_denied_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403)

    client = ConfluenceClient(_settings(), transport=httpx.MockTransport(handler))

    async def run():
        try:
            with pytest.raises(ConfluenceError) as error:
                await client.get_page("42", "KBR")
            assert not error.value.retryable
        finally:
            await client.close()

    asyncio.run(run())
    assert len(calls) == 1


def test_opt_in_space_discovery_paginates_all_visible_spaces_without_local_filter():
    requested = []

    def handler(request):
        requested.append(request.url)
        assert request.headers["Authorization"] == "Bearer test-token"
        first = len(requested) == 1
        return httpx.Response(200, json={
            "results": [{"key": "KBR" if first else "OTHER", "name": "Visible space"}],
            "_links": {"next": "/rest/api/space?start=1"} if first else {},
        })

    async def run():
        client = ConfluenceClient(
            _settings(confluence_max_results=1), transport=httpx.MockTransport(handler),
        )
        try:
            spaces = await client.list_spaces(include_all_accessible=True)
            assert [space["key"] for space in spaces] == ["KBR", "OTHER"]
            # Listing visible metadata does not grant permission to fetch other spaces.
            with pytest.raises(ConfluenceError, match="allowlist"):
                await client.get_page("42", "OTHER")
        finally:
            await client.close()

    asyncio.run(run())
    assert len(requested) == 2


def test_all_accessible_discovery_still_rejects_permission_denied():
    async def run():
        client = ConfluenceClient(
            _settings(), transport=httpx.MockTransport(lambda _: httpx.Response(403)),
        )
        try:
            with pytest.raises(ConfluenceError, match="permission was denied"):
                await client.list_spaces(include_all_accessible=True)
        finally:
            await client.close()

    asyncio.run(run())


def test_space_discovery_rejects_repeated_pagination():
    async def run():
        client = ConfluenceClient(
            _settings(), transport=httpx.MockTransport(lambda _: httpx.Response(
            200, json={"results": [], "_links": {"next": "/rest/api/space"}},
        )))
        try:
            with pytest.raises(ConfluenceError, match="did not advance"):
                await client.list_spaces(include_all_accessible=True)
        finally:
            await client.close()

    asyncio.run(run())
