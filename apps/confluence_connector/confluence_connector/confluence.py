from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlsplit

import httpx

from confluence_connector.config import Settings, read_secret, tls_verify


class ConfluenceError(RuntimeError):
    """Sanitized Confluence error safe to report to the RAG server."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ConfluenceClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.root = settings.confluence_url.strip().rstrip("/")
        self.allowed_spaces = {item.casefold(): item for item in settings.allowed_spaces}
        auth: httpx.Auth | None = None
        headers = {"Accept": "application/json", "User-Agent": "rag-confluence-connector/0.1"}
        if settings.confluence_auth_mode == "basic":
            auth = httpx.BasicAuth(
                settings.confluence_username.strip(),
                read_secret(settings.confluence_token, settings.confluence_token_file, "token"),
            )
        elif settings.confluence_auth_mode == "bearer":
            token = read_secret(
                settings.confluence_token,
                settings.confluence_token_file,
                "token",
            )
            headers["Authorization"] = f"Bearer {token}"
        else:
            cookie = (
                settings.confluence_cookie_file.expanduser().read_text(encoding="utf-8").strip()
            )
            if not cookie or "\n" in cookie or "\r" in cookie:
                raise RuntimeError("Confluence cookie file must contain one Cookie header value")
            headers["Cookie"] = cookie
        self.client = httpx.AsyncClient(
            auth=auth,
            headers=headers,
            timeout=settings.confluence_timeout_seconds,
            verify=tls_verify(settings.confluence_verify_ssl, settings.confluence_ca_file),
            follow_redirects=False,
            transport=transport,
        )

    async def close(self) -> None:
        await self.client.aclose()

    def _api_url(self, path: str) -> str:
        return f"{self.root}/rest/api/{path.lstrip('/')}"

    def _allowed(self, space_key: str) -> bool:
        return space_key.casefold() in self.allowed_spaces

    def _require_allowed(self, space_key: str) -> None:
        if not self._allowed(space_key):
            raise ConfluenceError("Requested space is outside the local connector allowlist")

    async def _get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], httpx.Response]:
        for attempt in range(self.settings.confluence_request_attempts):
            try:
                return await self._get_once(url, params=params)
            except ConfluenceError as exc:
                if not exc.retryable or attempt + 1 == self.settings.confluence_request_attempts:
                    raise
                await asyncio.sleep(self.settings.confluence_retry_delay_seconds * 2**attempt)
        raise AssertionError("Confluence request attempts must be positive")

    async def _get_once(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], httpx.Response]:
        expected = urlsplit(self.root)
        actual = urlsplit(url)
        if actual.scheme != expected.scheme or actual.netloc != expected.netloc:
            raise ConfluenceError("Refusing a Confluence request to an unexpected host")
        try:
            response = await self.client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise ConfluenceError("Confluence is unavailable", retryable=True) from exc
        if response.status_code in {301, 302, 303, 307, 308}:
            raise ConfluenceError("Confluence returned an unexpected redirect")
        if response.status_code in {401, 403}:
            raise ConfluenceError("Confluence authentication or page permission was denied")
        if response.status_code == 404:
            raise ConfluenceError("Confluence page or space was not found")
        if response.status_code >= 400:
            raise ConfluenceError(
                f"Confluence request failed with status {response.status_code}",
                retryable=response.status_code in {408, 429, 500, 502, 503, 504},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ConfluenceError("Confluence returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ConfluenceError("Confluence returned an unexpected response")
        return payload, response

    def _next(self, payload: dict[str, Any], response: httpx.Response) -> str | None:
        next_url = response.links.get("next", {}).get("url")
        links = payload.get("_links")
        if not next_url and isinstance(links, dict):
            next_url = links.get("next")
        if not isinstance(next_url, str) or not next_url:
            return None
        resolved = urljoin(f"{self.root}/", next_url)
        expected = urlsplit(self.root)
        actual = urlsplit(resolved)
        if actual.scheme != expected.scheme or actual.netloc != expected.netloc:
            raise ConfluenceError("Confluence pagination attempted to leave the configured host")
        return resolved

    def _web_url(self, payload: dict[str, Any]) -> str | None:
        links = payload.get("_links")
        if not isinstance(links, dict):
            return None
        web_ui = links.get("webui")
        if not isinstance(web_ui, str) or not web_ui:
            return None
        base = links.get("base")
        if not isinstance(base, str) or not base:
            base = self.root
        expected = urlsplit(self.root)
        actual = urlsplit(base)
        if actual.scheme != expected.scheme or actual.netloc != expected.netloc:
            return None
        return f"{base.rstrip('/')}/{web_ui.lstrip('/')}"

    @staticmethod
    def _space_key(payload: dict[str, Any]) -> str:
        space = payload.get("space")
        return str(space.get("key") or "").strip() if isinstance(space, dict) else ""

    def _page_node(self, payload: dict[str, Any], parent_id: str | None) -> dict[str, Any]:
        page_id = str(payload.get("id") or "").strip()
        title = str(payload.get("title") or "").strip()
        space_key = self._space_key(payload)
        version_data = payload.get("version")
        version = version_data.get("number") if isinstance(version_data, dict) else None
        children = payload.get("children")
        page_children = children.get("page") if isinstance(children, dict) else None
        child_size = page_children.get("size") if isinstance(page_children, dict) else None
        if not page_id or not title or not space_key:
            raise ConfluenceError("Confluence page metadata is incomplete")
        self._require_allowed(space_key)
        return {
            "id": page_id,
            "title": title,
            "space_key": space_key,
            "version": str(version or "unknown"),
            "parent_id": parent_id,
            "has_children": bool(child_size),
            "web_url": self._web_url(payload),
        }

    async def check_connection(self) -> None:
        await self._get(self._api_url("space"), params={"limit": 1})

    async def list_spaces(self, *, include_all_accessible: bool = False) -> list[dict[str, Any]]:
        spaces: list[dict[str, Any]] = []
        visited: set[str] = set()
        seen_keys: set[str] = set()
        url: str | None = self._api_url("space")
        params: dict[str, Any] | None = {
            "limit": min(100, self.settings.confluence_max_results),
            "expand": "homepage",
        }
        while url and (
            include_all_accessible or len(spaces) < self.settings.confluence_max_results
        ):
            if url in visited:
                raise ConfluenceError("Confluence space pagination did not advance")
            visited.add(url)
            payload, response = await self._get(url, params=params)
            params = None
            results = payload.get("results")
            if not isinstance(results, list):
                raise ConfluenceError("Confluence space listing has an invalid shape")
            for item in results:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key") or "").strip()
                name = str(item.get("name") or "").strip()
                if not key or not name or (not include_all_accessible and not self._allowed(key)):
                    continue
                if key.casefold() in seen_keys:
                    continue
                seen_keys.add(key.casefold())
                homepage = item.get("homepage")
                homepage_id = (
                    str(homepage.get("id"))
                    if isinstance(homepage, dict) and homepage.get("id")
                    else None
                )
                spaces.append(
                    {
                        "key": key,
                        "name": name,
                        "homepage_id": homepage_id,
                        "web_url": self._web_url(item),
                    }
                )
            url = self._next(payload, response)
        return spaces

    async def list_space_pages(self, space_key: str, start: int = 0) -> dict[str, Any]:
        """Read one bounded page of all current pages, including non-homepage trees."""
        self._require_allowed(space_key)
        if type(start) is not int or start < 0:
            raise ValueError("Invalid space pagination offset")
        payload, response = await self._get(
            self._api_url("content"),
            params={
                "spaceKey": space_key, "type": "page", "status": "current",
                "start": start, "limit": 100, "expand": "version,space,ancestors",
            },
        )
        raw_pages = payload.get("results")
        if not isinstance(raw_pages, list) or len(raw_pages) > 100:
            raise ConfluenceError("Confluence space listing has an invalid shape")
        pages = []
        for item in raw_pages:
            if not isinstance(item, dict):
                raise ConfluenceError("Confluence space page metadata is invalid")
            node = self._page_node(item, None)
            if node["space_key"].casefold() != space_key.casefold():
                raise ConfluenceError("Confluence returned a page from another space")
            pages.append(node)
        next_url = self._next(payload, response)
        next_start = None
        if next_url:
            try:
                next_start = int(parse_qs(urlsplit(next_url).query)["start"][0])
            except (KeyError, ValueError, IndexError) as exc:
                raise ConfluenceError("Confluence returned an invalid pagination offset") from exc
            if next_start <= start or not pages:
                raise ConfluenceError("Confluence space pagination did not advance")
        return {"pages": pages, "next_start": next_start}

    async def list_children(
        self,
        space_key: str,
        parent_id: str | None,
    ) -> list[dict[str, Any]]:
        self._require_allowed(space_key)
        if parent_id is None:
            encoded_space = quote(space_key, safe="")
            space, _ = await self._get(
                self._api_url(f"space/{encoded_space}"),
                params={"expand": "homepage"},
            )
            homepage = space.get("homepage")
            homepage_id = (
                str(homepage.get("id") or "").strip() if isinstance(homepage, dict) else ""
            )
            if not homepage_id:
                return []
            page, _ = await self._get(
                self._api_url(f"content/{quote(homepage_id, safe='')}"),
                params={"expand": "version,space,ancestors,children.page"},
            )
            node = self._page_node(page, None)
            if node["space_key"].casefold() != space_key.casefold():
                raise ConfluenceError("Confluence returned a homepage from another space")
            return [node]

        url: str | None = self._api_url(f"content/{quote(parent_id, safe='')}/child/page")
        params: dict[str, Any] | None = {
            "limit": min(100, self.settings.confluence_max_results),
            "expand": "version,space,ancestors,children.page",
        }
        pages: list[dict[str, Any]] = []
        while url and len(pages) < self.settings.confluence_max_results:
            payload, response = await self._get(url, params=params)
            params = None
            results = payload.get("results")
            if not isinstance(results, list):
                raise ConfluenceError("Confluence child page listing has an invalid shape")
            for item in results:
                if not isinstance(item, dict):
                    continue
                node = self._page_node(item, parent_id)
                if node["space_key"].casefold() != space_key.casefold():
                    raise ConfluenceError("Confluence returned a child from another space")
                pages.append(node)
            url = self._next(payload, response)
        return pages

    async def get_page(self, page_id: str, expected_space: str) -> dict[str, Any]:
        self._require_allowed(expected_space)
        payload, _ = await self._get(
            self._api_url(f"content/{quote(page_id, safe='')}"),
            params={"expand": "body.storage,version,space,ancestors,metadata.labels"},
        )
        returned_id = str(payload.get("id") or "").strip()
        title = str(payload.get("title") or "").strip()
        space_key = self._space_key(payload)
        body = payload.get("body")
        storage = body.get("storage") if isinstance(body, dict) else None
        storage_html = storage.get("value") if isinstance(storage, dict) else None
        version_data = payload.get("version")
        version = version_data.get("number") if isinstance(version_data, dict) else None
        if returned_id != page_id or not title or not isinstance(storage_html, str):
            raise ConfluenceError("Confluence page response is incomplete or mismatched")
        if space_key.casefold() != expected_space.casefold() or not self._allowed(space_key):
            raise ConfluenceError("Confluence page belongs to an unexpected space")
        ancestors = payload.get("ancestors")
        metadata = payload.get("metadata")
        labels = metadata.get("labels") if isinstance(metadata, dict) else None
        label_results = labels.get("results") if isinstance(labels, dict) else None
        return {
            "id": returned_id,
            "title": title,
            "space_key": space_key,
            "version": str(version or "unknown"),
            "parent_id": None,
            "has_children": False,
            "web_url": self._web_url(payload),
            "storage_html": storage_html,
            "ancestors": [
                str(item["title"])
                for item in (ancestors if isinstance(ancestors, list) else [])
                if isinstance(item, dict) and item.get("title")
            ],
            "labels": [
                str(item["name"])
                for item in (label_results if isinstance(label_results, list) else [])
                if isinstance(item, dict) and item.get("name")
            ],
        }
