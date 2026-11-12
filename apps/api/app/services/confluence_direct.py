"""Direct server-account access, bounded by an explicit space allowlist.

Authentication, safe pagination and sanitised errors reuse the existing client.
Data Center uses REST content/space endpoints; Cloud uses the v2 API.
Personal request-credential imports and outbound connector jobs stay separate.
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import parse_qs, quote, urlsplit

from app.core.config import settings
from app.services.confluence import ConfluenceClient, ConfluenceError, ConfluencePage


def selected_confluence_mode() -> Literal["direct", "connector"]:
    if settings.confluence_connection_mode != "auto":
        return settings.confluence_connection_mode
    return "connector" if settings.confluence_connector_enabled else "direct"


class DirectConfluenceClient(ConfluenceClient):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._email = self._email.strip()
        self._token = self._token.strip()
        self.allowed_spaces = {
            key.casefold(): key for key in settings.confluence_allowed_space_keys
        }
        self._cloud_space_keys: dict[str, str] = {}

    @property
    def missing_configuration(self) -> list[str]:
        required = {
            "CONFLUENCE_ENABLED": self.enabled,
            "CONFLUENCE_BASE_URL": self.host,
            "CONFLUENCE_ALLOWED_SPACES": self.allowed_spaces,
        }
        if self._auth_mode == "basic":
            required.update(CONFLUENCE_EMAIL=self._email, CONFLUENCE_API_TOKEN=self._token)
        else:
            required["CONFLUENCE_ACCESS_TOKEN"] = self._token
        return [name for name, value in required.items() if not value]

    @property
    def readiness_reason(self) -> str | None:
        if not self.enabled:
            return "disabled"
        if not self.host or not self.has_credentials:
            return "credentials_missing"
        if not self.allowed_spaces:
            return "allowed_spaces_missing"
        return None

    def require_allowed(self, space_key: str | None) -> None:
        if not isinstance(space_key, str) or space_key.casefold() not in self.allowed_spaces:
            raise ConfluenceError("Requested page or space is outside the server allowlist")

    def rest_url(self, path: str) -> str:
        return f"{self._root}/rest/api/{path.lstrip('/')}"

    async def _cloud_page_space(self, payload: dict[str, Any]) -> dict[str, Any]:
        space_id = str(payload.get("spaceId") or "")
        if not space_id:
            raise ConfluenceError("Confluence page response is missing its space")
        if space_id not in self._cloud_space_keys:
            space, _ = await self._get(self._api_url(f"spaces/{quote(space_id, safe='')}"))
            if str(space.get("id") or "") != space_id:
                raise ConfluenceError("Confluence returned a different space than requested")
            self.require_allowed(space.get("key"))
            self._cloud_space_keys[space_id] = space["key"]
        return {**payload, "space": {"key": self._cloud_space_keys[space_id]}}

    async def get_page(self, page_id: str) -> ConfluencePage:
        if self._deployment == "cloud":
            payload, _ = await self._get(
                self._api_url(f"pages/{quote(page_id, safe='')}"),
                params={"body-format": "storage"},
            )
            payload = await self._cloud_page_space(payload)
        else:
            payload, _ = await self._get(
                self.rest_url(f"content/{quote(page_id, safe='')}"),
                params={"expand": "body.storage,version,space,ancestors,metadata.labels"},
            )
        try:
            page = self._page(payload)
        except (AttributeError, TypeError, KeyError) as exc:
            raise ConfluenceError("Confluence page metadata is invalid") from exc
        if page.id != page_id:
            raise ConfluenceError("Confluence returned a different page id than requested")
        self.require_allowed(page.space_key)
        return page

    async def space(self, space_key: str) -> dict[str, Any]:
        self.require_allowed(space_key)
        if self._deployment == "cloud":
            listing, _ = await self._get(
                self._api_url("spaces"), params={"keys": space_key, "limit": 2}
            )
            candidates = listing.get("results")
            payload = (
                next(
                    (
                        item
                        for item in candidates
                        if isinstance(item, dict)
                        and str(item.get("key") or "").casefold() == space_key.casefold()
                    ),
                    None,
                )
                if isinstance(candidates, list)
                else None
            )
            if not payload or not payload.get("id"):
                raise ConfluenceError("Confluence space was not found")
            self._cloud_space_keys[str(payload["id"])] = payload["key"]
            payload = {**payload, "homepage": {"id": payload.get("homepageId")}}
        else:
            payload, _ = await self._get(
                self.rest_url(f"space/{quote(space_key, safe='')}"),
                params={"expand": "homepage"},
            )
        if str(payload.get("key") or "").casefold() != space_key.casefold():
            raise ConfluenceError("Confluence returned a different space than requested")
        if not payload.get("name"):
            raise ConfluenceError("Confluence space metadata is incomplete")
        return payload

    async def list_spaces(self) -> list[dict[str, Any]]:
        # Fetch only configured spaces rather than enumerate the service account.
        spaces = []
        for key in self.allowed_spaces.values():
            item = await self.space(key)
            homepage = item.get("homepage")
            spaces.append(
                {
                    "key": item["key"],
                    "name": item["name"],
                    "homepage_id": str(homepage["id"])
                    if isinstance(homepage, dict) and homepage.get("id")
                    else None,
                    "web_url": self._source_url(item),
                }
            )
        return spaces

    def page_node(
        self, item: dict[str, Any], space_key: str, parent_id: str | None
    ) -> dict[str, Any]:
        space = item.get("space")
        actual_space = str(space.get("key") or "") if isinstance(space, dict) else ""
        self.require_allowed(actual_space)
        if actual_space.casefold() != space_key.casefold():
            raise ConfluenceError("Confluence returned a page from another space")
        if not item.get("id") or not isinstance(item.get("title"), str) or not item["title"]:
            raise ConfluenceError("Confluence page metadata is incomplete")
        version = item.get("version")
        children = item.get("children")
        page_children = children.get("page") if isinstance(children, dict) else None
        return {
            "id": str(item["id"]),
            "title": item["title"],
            "space_key": actual_space,
            "version": str(version.get("number") or "unknown")
            if isinstance(version, dict)
            else "unknown",
            "parent_id": parent_id,
            # Cloud v2 omits a child count. Let the user expand and fetch lazily.
            "has_children": bool(page_children.get("size"))
            if isinstance(page_children, dict)
            else self._deployment == "cloud",
            "web_url": self._source_url(item),
        }

    async def list_children(self, space_key: str, parent_id: str | None) -> list[dict[str, Any]]:
        self.require_allowed(space_key)
        if parent_id is None:
            space = await self.space(space_key)
            homepage = space.get("homepage")
            home_id = str(homepage.get("id") or "") if isinstance(homepage, dict) else ""
            if not home_id:
                return []
            payload, _ = await self._get(
                self._api_url(f"pages/{quote(home_id, safe='')}")
                if self._deployment == "cloud"
                else self.rest_url(f"content/{quote(home_id, safe='')}"),
                params={}
                if self._deployment == "cloud"
                else {"expand": "version,space,children.page"},
            )
            if self._deployment == "cloud":
                payload = await self._cloud_page_space(payload)
            if str(payload.get("id") or "") != home_id:
                raise ConfluenceError("Confluence returned a different page id than requested")
            return [self.page_node(payload, space_key, None)]
        # Check the actual parent space even when callers submit a valid allowed key.
        parent, _ = await self._get(
            self._api_url(f"pages/{quote(parent_id, safe='')}")
            if self._deployment == "cloud"
            else self.rest_url(f"content/{quote(parent_id, safe='')}"),
            params={} if self._deployment == "cloud" else {"expand": "version,space"},
        )
        if self._deployment == "cloud":
            parent = await self._cloud_page_space(parent)
        if str(parent.get("id") or "") != parent_id:
            raise ConfluenceError("Confluence returned a different page id than requested")
        self.page_node(parent, space_key, None)
        url: str | None = (
            self._api_url(f"pages/{quote(parent_id, safe='')}/children")
            if self._deployment == "cloud"
            else self.rest_url(f"content/{quote(parent_id, safe='')}/child/page")
        )
        params: dict[str, Any] | None = {"limit": 100}
        if self._deployment != "cloud":
            params["expand"] = "version,space,children.page"
        pages = []
        visited: set[str] = set()
        while url:
            if url in visited:
                raise ConfluenceError("Confluence child pagination did not advance")
            visited.add(url)
            payload, response = await self._get(url, params=params)
            params = None
            results = payload.get("results")
            if not isinstance(results, list):
                raise ConfluenceError("Confluence child page listing has an invalid shape")
            for item in results:
                if not isinstance(item, dict):
                    raise ConfluenceError("Confluence child page metadata is invalid")
                if self._deployment == "cloud":
                    item = await self._cloud_page_space(item)
                pages.append(self.page_node(item, space_key, parent_id))
            if len(pages) > self._maximum_pages:
                raise ConfluenceError("Too many child pages; use the paginated space listing")
            url = self._safe_next_url(payload, response)
        return pages

    async def list_space_pages(
        self, space_key: str, start: int = 0, cursor: str | None = None
    ) -> dict[str, Any]:
        self.require_allowed(space_key)
        if self._deployment == "cloud":
            space = await self.space(space_key)
            params: dict[str, Any] = {"limit": 100, "status": "current"}
            if cursor:
                params["cursor"] = cursor
            payload, response = await self._get(
                self._api_url(f"spaces/{quote(str(space['id']), safe='')}/pages"), params=params
            )
            items = payload.get("results")
            if (
                not isinstance(items, list)
                or len(items) > 100
                or any(not isinstance(item, dict) for item in items)
            ):
                raise ConfluenceError("Confluence space listing has an invalid shape")
            pages = [
                self.page_node(await self._cloud_page_space(item), space_key, None)
                for item in items
            ]
            next_url = self._safe_next_url(payload, response)
            next_cursor = (
                parse_qs(urlsplit(next_url).query).get("cursor", [None])[0] if next_url else None
            )
            if next_url and (not next_cursor or next_cursor == cursor or not pages):
                raise ConfluenceError("Confluence space pagination did not advance")
            return {
                "pages": pages,
                "next_start": start + len(pages) if next_cursor else None,
                "next_cursor": next_cursor,
            }
        payload, response = await self._get(
            self.rest_url("content"),
            params={
                "spaceKey": space_key,
                "type": "page",
                "status": "current",
                "start": start,
                "limit": 100,
                "expand": "version,space",
            },
        )
        items = payload.get("results")
        if (
            not isinstance(items, list)
            or len(items) > 100
            or any(not isinstance(item, dict) for item in items)
        ):
            raise ConfluenceError("Confluence space listing has an invalid shape")
        pages = [self.page_node(item, space_key, None) for item in items]
        next_url = self._safe_next_url(payload, response)
        next_start = None
        if next_url:
            try:
                next_start = int(parse_qs(urlsplit(next_url).query)["start"][0])
            except (KeyError, ValueError, IndexError) as exc:
                raise ConfluenceError("Confluence returned an invalid pagination offset") from exc
            if next_start <= start or not pages:
                raise ConfluenceError("Confluence space pagination did not advance")
        return {"pages": pages, "next_start": next_start}
