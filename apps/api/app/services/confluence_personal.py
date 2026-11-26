"""Live Confluence access using the personal token of an authenticated RAG session."""

import hashlib
import json
import ssl
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import settings
from app.core.security import Principal
from app.services.confluence import ConfluenceError
from app.services.confluence_direct import DirectConfluenceClient

redis = Redis.from_url(settings.redis_url, decode_responses=True)


def enabled() -> bool:
    origin = urlsplit(settings.confluence_base_url)
    return bool(
        settings.auth_enabled
        and settings.auth_mode == "password"
        and settings.auth_cookie_secure
        and settings.confluence_personal_socket
        and settings.confluence_personal_key_file
        and settings.confluence_deployment == "data_center"
        and settings.confluence_auth_mode == "bearer"
        and origin.scheme == "https"
        and origin.hostname
        and not origin.username
        and not origin.password
        and not origin.query
        and not origin.fragment
    )


def session_key(request: Request, principal: Principal) -> str:
    sid = getattr(request.state, "auth_session_hash", None)
    if not enabled() or not sid:
        raise HTTPException(403, "Личное подключение Confluence недоступно для этого сеанса.")
    scope = json.dumps([principal.tenant_id, principal.subject, sid], separators=(",", ":"))
    return "rag:confluence:session:" + hashlib.sha256(scope.encode()).hexdigest()


@lru_cache(maxsize=1)
def cipher(key_file: str) -> Fernet:
    return Fernet(Path(key_file).read_bytes().strip())


async def get_token(key: str) -> str | None:
    try:
        value = await redis.get(key)
        if not value:
            return None
        payload = json.loads(cipher(settings.confluence_personal_key_file).decrypt(value.encode()))
    except (InvalidToken, ValueError):
        return None
    except (OSError, RedisError) as exc:
        raise HTTPException(503, "Хранилище подключений временно недоступно.") from exc
    return (
        payload.get("token")
        if isinstance(payload, dict) and payload.get("session") == key
        else None
    )


async def save_token(key: str, token: str, expires_at: datetime) -> None:
    ttl = int((expires_at - datetime.now(UTC)).total_seconds())
    if ttl <= 0:
        raise HTTPException(401, "Сеанс RAG завершён.")
    try:
        data = cipher(settings.confluence_personal_key_file).encrypt(
            json.dumps({"session": key, "token": token}).encode()
        )
        await redis.set(key, data, ex=ttl)
    except (OSError, ValueError, RedisError) as exc:
        raise HTTPException(503, "Не удалось сохранить подключение в этом сеансе.") from exc


async def forget_token(key: str) -> None:
    try:
        await redis.delete(key)
    except RedisError as exc:
        raise HTTPException(503, "Хранилище подключений временно недоступно.") from exc


class PersonalConfluenceClient(DirectConfluenceClient):
    def __init__(self, token: str, requested_spaces=(), *, transport=None):
        if not token.strip() or not enabled():
            raise ConfluenceError("Personal Confluence access is unconfigured")
        # TLS ends at the configured Confluence origin, across the server's private socket.
        transport = transport or httpx.AsyncHTTPTransport(
            uds=settings.confluence_personal_socket,
            trust_env=False,
            verify=ssl.create_default_context(cafile=settings.confluence_personal_ca_file or None),
        )
        super().__init__(email="", token=token, transport=transport)
        # The caller narrows each operation to the selected spaces. Confluence enforces
        # the personal token's permissions on every upstream page and space request.
        self.allowed_spaces = {s.casefold(): s for s in requested_spaces}

    @property
    def enabled(self) -> bool:
        return enabled()

    async def list_spaces(self) -> list[dict]:
        url = self.rest_url("space")
        path = urlsplit(url).path
        params = {"limit": 100, "expand": "homepage"}
        seen_urls, spaces = set(), {}
        while url:
            if url in seen_urls or len(seen_urls) >= 500:
                raise ConfluenceError("Confluence returned incomplete pagination")
            seen_urls.add(url)
            data, response = await self._get(url, params=params)
            params = None
            items = data.get("results")
            if not isinstance(items, list):
                raise ConfluenceError("Confluence returned an invalid space listing")
            for item in items:
                if not isinstance(item, dict) or not all(
                    isinstance(item.get(field), str) and item[field] for field in ("key", "name")
                ):
                    raise ConfluenceError("Confluence returned invalid space metadata")
                homepage = item.get("homepage") or {}
                if not isinstance(homepage, dict):
                    raise ConfluenceError("Confluence returned invalid homepage metadata")
                spaces[item["key"].casefold()] = {
                    "key": item["key"],
                    "name": item["name"],
                    "homepage_id": str(homepage["id"]) if homepage.get("id") else None,
                    "web_url": self._source_url(item),
                }
            url = self._safe_next_url(data, response)
            if url and urlsplit(url).path.rstrip("/") != path.rstrip("/"):
                raise ConfluenceError("Unexpected Confluence space pagination target")
        return list(spaces.values())
