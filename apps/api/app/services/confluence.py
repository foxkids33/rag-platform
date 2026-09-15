from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from app.core.config import settings


class ConfluenceError(RuntimeError):
    """A sanitized Confluence transport or response error."""


@dataclass(frozen=True, slots=True)
class ConfluencePage:
    id: str
    title: str
    storage_html: str
    version: str
    source_url: str | None
    space_key: str | None = None
    ancestors: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)


class ConfluenceClient:
    def __init__(
        self,
        *,
        email: str | None = None,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        configured_root = settings.confluence_base_url.strip().rstrip("/")
        if settings.confluence_deployment == "cloud" and configured_root.endswith("/wiki"):
            configured_root = configured_root[:-5]
        self._root = configured_root
        self._deployment = settings.confluence_deployment
        self._auth_mode = settings.confluence_auth_mode
        self._email = settings.confluence_email if email is None else email.strip()
        configured_token = (
            settings.confluence_api_token.get_secret_value()
            if self._auth_mode == "basic"
            else settings.confluence_access_token.get_secret_value()
        )
        self._token = configured_token if token is None else token
        self._timeout = settings.confluence_timeout_seconds
        self._verify_ssl = settings.confluence_verify_ssl
        self._maximum_pages = settings.confluence_max_pages_per_import
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return settings.confluence_enabled

    @property
    def host(self) -> str | None:
        return urlsplit(self._root).hostname if self._root else None

    @property
    def has_credentials(self) -> bool:
        if self._auth_mode == "basic":
            return bool(self._email and self._token)
        return bool(self._token)

    def _api_url(self, path: str) -> str:
        prefix = "/wiki/api/v2" if self._deployment == "cloud" else "/rest/api"
        return f"{self._root}{prefix}/{path.lstrip('/')}"

    def _auth(self) -> tuple[httpx.Auth | None, dict[str, str]]:
        headers = {"Accept": "application/json"}
        if self._auth_mode == "basic":
            return (
                httpx.BasicAuth(self._email, self._token),
                headers,
            )
        headers["Authorization"] = f"Bearer {self._token}"
        return None, headers

    def _get_client(self) -> httpx.AsyncClient:
        if not self.enabled:
            raise ConfluenceError("Confluence integration is disabled")
        if not self.has_credentials:
            raise ConfluenceError("Confluence credentials are required")
        if self._client is None:
            auth, headers = self._auth()
            self._client = httpx.AsyncClient(
                auth=auth,
                headers=headers,
                timeout=self._timeout,
                verify=self._verify_ssl,
                follow_redirects=False,
                transport=self._transport,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], httpx.Response]:
        try:
            response = await self._get_client().get(url, params=params)
        except httpx.HTTPError as exc:
            raise ConfluenceError("Confluence is unavailable") from exc

        if response.status_code in {301, 302, 303, 307, 308}:
            raise ConfluenceError("Confluence returned an unexpected redirect")
        if response.status_code in {401, 403}:
            raise ConfluenceError("Confluence authentication or page permission was denied")
        if response.status_code == 404:
            raise ConfluenceError("Confluence page or space was not found")
        if response.status_code >= 400:
            raise ConfluenceError(
                f"Confluence request failed with status {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ConfluenceError("Confluence returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ConfluenceError("Confluence returned an unexpected response")
        return payload, response

    def _safe_next_url(
        self,
        payload: dict[str, Any],
        response: httpx.Response,
    ) -> str | None:
        next_url = response.links.get("next", {}).get("url")
        if not next_url:
            links = payload.get("_links")
            if isinstance(links, dict):
                next_url = links.get("next")
        if not isinstance(next_url, str) or not next_url:
            return None

        resolved = urljoin(f"{self._root}/", next_url)
        expected = urlsplit(self._root)
        actual = urlsplit(resolved)
        if actual.scheme != expected.scheme or actual.netloc != expected.netloc:
            raise ConfluenceError("Confluence pagination attempted to leave the configured host")
        return resolved

    def _source_url(self, payload: dict[str, Any]) -> str | None:
        links = payload.get("_links")
        if not isinstance(links, dict):
            return None
        web_ui = links.get("webui")
        if not isinstance(web_ui, str) or not web_ui:
            return None
        base = links.get("base")
        if not isinstance(base, str) or not base:
            base = f"{self._root}/wiki" if self._deployment == "cloud" else self._root
        expected = urlsplit(self._root)
        actual = urlsplit(base)
        if actual.scheme != expected.scheme or actual.netloc != expected.netloc:
            return None
        return f"{base.rstrip('/')}/{web_ui.lstrip('/')}"

    def _page(self, payload: dict[str, Any]) -> ConfluencePage:
        page_id = str(payload.get("id") or "").strip()
        title = str(payload.get("title") or "").strip()
        body = payload.get("body")
        storage = body.get("storage") if isinstance(body, dict) else None
        storage_html = storage.get("value") if isinstance(storage, dict) else None
        version_data = payload.get("version")
        version = version_data.get("number") if isinstance(version_data, dict) else None
        if not page_id or not title or not isinstance(storage_html, str):
            raise ConfluenceError("Confluence page response is missing id, title, or storage body")
        return ConfluencePage(
            id=page_id,
            title=title,
            storage_html=storage_html,
            version=str(version or "unknown"),
            source_url=self._source_url(payload),
            space_key=(payload.get("space") or {}).get("key"),
            ancestors=[str(item["title"]) for item in payload.get("ancestors", []) if item.get("title")],
            labels=[str(item["name"]) for item in (payload.get("metadata") or {}).get("labels", {}).get("results", []) if item.get("name")],
        )

    async def get_page(self, page_id: str) -> ConfluencePage:
        encoded_page_id = quote(page_id, safe="")
        if self._deployment == "cloud":
            payload, _ = await self._get(
                self._api_url(f"pages/{encoded_page_id}"),
                params={"body-format": "storage"},
            )
        else:
            payload, _ = await self._get(
                self._api_url(f"content/{encoded_page_id}"),
                params={"expand": "body.storage,version,space,ancestors,metadata.labels"},
            )
        page = self._page(payload)
        if page.id != page_id:
            raise ConfluenceError("Confluence returned a different page id than requested")
        return page

    async def check_connection(self) -> None:
        path = "spaces" if self._deployment == "cloud" else "space"
        await self._get(self._api_url(path), params={"limit": 1})

    async def list_space_page_ids(self, space_key: str) -> list[str]:
        maximum = self._maximum_pages
        if self._deployment == "cloud":
            spaces, _ = await self._get(
                self._api_url("spaces"),
                params={"keys": space_key, "limit": 2},
            )
            candidates = spaces.get("results")
            if not isinstance(candidates, list) or not candidates:
                raise ConfluenceError("Confluence space was not found")
            matching_space = next(
                (
                    candidate
                    for candidate in candidates
                    if isinstance(candidate, dict)
                    and str(candidate.get("key") or "").casefold()
                    == space_key.casefold()
                ),
                None,
            )
            if matching_space is None:
                raise ConfluenceError("Confluence returned a different space than requested")
            space_id = str(matching_space.get("id") or "")
            if not space_id:
                raise ConfluenceError("Confluence space response is missing an id")
            url = self._api_url(f"spaces/{space_id}/pages")
            params: dict[str, Any] | None = {"limit": min(100, maximum)}
            page_ids: list[str] = []
            while url and len(page_ids) < maximum:
                payload, response = await self._get(url, params=params)
                params = None
                results = payload.get("results")
                if not isinstance(results, list):
                    raise ConfluenceError("Confluence page listing has an invalid shape")
                page_ids.extend(
                    str(item["id"])
                    for item in results
                    if isinstance(item, dict) and item.get("id")
                )
                url = self._safe_next_url(payload, response)
            if url or len(page_ids) > maximum:
                raise ConfluenceError("Space exceeds import limit; use the paginated snapshot/sync adapter")
            return page_ids[:maximum]

        page_ids = []
        start = 0
        while len(page_ids) < maximum:
            limit = min(100, maximum - len(page_ids))
            payload, response = await self._get(
                self._api_url("content"),
                params={
                    "type": "page",
                    "spaceKey": space_key,
                    "start": start,
                    "limit": limit,
                },
            )
            results = payload.get("results")
            if not isinstance(results, list):
                raise ConfluenceError("Confluence page listing has an invalid shape")
            page_ids.extend(
                str(item["id"])
                for item in results
                if isinstance(item, dict) and item.get("id")
            )
            size = int(payload.get("size") or len(results))
            next_url = self._safe_next_url(payload, response)
            if len(page_ids) >= maximum and next_url:
                raise ConfluenceError("Space exceeds import limit; use the paginated snapshot/sync adapter")
            if not results or (size < limit and not next_url):
                break
            start += size
        return page_ids[:maximum]


confluence = ConfluenceClient()
