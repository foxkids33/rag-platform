from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import suppress
from typing import Any

import httpx

from confluence_connector import __version__
from confluence_connector.config import Settings, normalize_rag_url, tls_verify
from confluence_connector.confluence import ConfluenceClient, ConfluenceError

logger = logging.getLogger("rag-confluence-connector")
PAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
CAPABILITIES = ["LIST_SPACES", "LIST_CHILDREN", "LIST_SPACE_PAGES", "IMPORT_PAGES"]


class RagClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = normalize_rag_url(settings.rag_url)
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                settings.rag_request_timeout_seconds,
                read=max(settings.rag_request_timeout_seconds, settings.rag_poll_seconds + 10),
            ),
            verify=tls_verify(settings.rag_verify_ssl, settings.rag_ca_file),
            follow_redirects=False,
            headers={"User-Agent": f"rag-confluence-connector/{__version__}"},
        )
        self.connector_id: str | None = None
        self.credential: str | None = None
        self.heartbeat_seconds = 30.0

    async def close(self) -> None:
        await self.client.aclose()

    def _headers(self) -> dict[str, str]:
        if not self.credential:
            raise RuntimeError("Connector is not enrolled")
        return {"Authorization": f"Bearer {self.credential}"}

    def _load_credential(self) -> bool:
        path = self.settings.credential_path
        if not path.exists():
            if self.settings.rag_credential_file is not None:
                return False
            # Reuse the legacy default only when its destination is explicitly bound
            # to this server. Keep it intact so an old installation still works.
            legacy = self.settings.legacy_credential_path
            if not legacy.is_file():
                return False
            try:
                payload = json.loads(legacy.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    return False
                bound_url = payload.get("rag_url")
                if not isinstance(bound_url, str) or normalize_rag_url(bound_url) != self.root:
                    return False
                connector_id, credential = payload.get("connector_id"), payload.get("credential")
                if not isinstance(connector_id, str) or not isinstance(credential, str):
                    return False
                if not connector_id or not credential:
                    return False
            except (OSError, ValueError):
                return False
            self._save_credential(connector_id, credential)
            self.connector_id, self.credential = connector_id, credential
            logger.info("Copied matching legacy credential into destination-scoped storage")
            return True
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("RAG connector credential file is invalid") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("RAG connector credential file is invalid")
        connector_id = payload.get("connector_id")
        credential = payload.get("credential")
        if (
            not isinstance(connector_id, str)
            or not isinstance(credential, str)
            or not connector_id
            or not credential
        ):
            raise RuntimeError("RAG connector credential file is incomplete")
        bound_url = payload.get("rag_url")
        try:
            matches = isinstance(bound_url, str) and normalize_rag_url(bound_url) == self.root
        except ValueError:
            matches = False
        if not matches:
            raise RuntimeError(
                "Saved credential belongs to another RAG_URL; use a separate credential file"
            )
        self.connector_id = connector_id
        self.credential = credential
        return True

    def _save_credential(self, connector_id: str, credential: str) -> None:
        path = self.settings.credential_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(
                    {"connector_id": connector_id, "credential": credential, "rag_url": self.root},
                    stream,
                )
                stream.write("\n")
            os.replace(temporary, path)
            path.chmod(0o600)
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()

    async def enroll(self, *, pilot: bool = False) -> None:
        if self._load_credential():
            return
        token = self.settings.rag_enrollment_token.get_secret_value().strip()
        if not token and pilot:
            response = await self.client.post(
                f"{self.root}/api/v1/integrations/confluence/connectors/enrollments",
                json={
                    "name": "Laptop Confluence Connector",
                    "allowed_spaces": self.settings.allowed_spaces,
                },
            )
            if response.status_code != 201:
                raise RuntimeError(
                    f"Pilot enrollment creation failed with HTTP {response.status_code}"
                )
            token = response.json()["enrollment_token"]
        if not token:
            raise RuntimeError("No saved RAG credential and RAG_ENROLLMENT_TOKEN is empty")
        response = await self.client.post(
            f"{self.root}/api/v1/connectors/enroll",
            json={
                "enrollment_token": token,
                "version": __version__,
                "capabilities": CAPABILITIES,
            },
        )
        if response.status_code != 200:
            raise RuntimeError(f"Connector enrollment failed with HTTP {response.status_code}")
        payload = response.json()
        connector_id = str(payload.get("connector_id") or "")
        credential = str(payload.get("credential") or "")
        if not connector_id or not credential:
            raise RuntimeError("RAG returned an invalid enrollment response")
        self._save_credential(connector_id, credential)
        self.connector_id = connector_id
        self.credential = credential
        self.heartbeat_seconds = max(5, min(30, float(payload.get("lease_seconds", 180)) / 3))
        logger.info("Connector enrollment completed: %s", connector_id)

    async def next_job(self) -> dict[str, Any] | None:
        if not self.connector_id:
            raise RuntimeError("Connector is not enrolled")
        response = await self.client.get(
            f"{self.root}/api/v1/connectors/{self.connector_id}/jobs/next",
            params={"wait": self.settings.rag_poll_seconds},
            headers=self._headers(),
        )
        if response.status_code == 204:
            return None
        if response.status_code == 401:
            raise RuntimeError("RAG rejected the connector credential")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("RAG returned an invalid connector job")
        return payload

    async def heartbeat(self, job: dict[str, Any] | None = None) -> None:
        if not self.connector_id:
            return
        payload: dict[str, Any] = {"version": __version__, "capabilities": CAPABILITIES}
        if job:
            payload.update({"job_id": job["job_id"], "lease_id": job["lease_id"]})
        response = await self.client.post(
            f"{self.root}/api/v1/connectors/{self.connector_id}/heartbeat",
            headers=self._headers(),
            json=payload,
        )
        response.raise_for_status()

    async def submit_result(self, job: dict[str, Any], result: dict[str, Any]) -> None:
        # Retry the same result when its acknowledgement was lost.
        for attempt in range(3):
            try:
                response = await self.client.post(
                    f"{self.root}/api/v1/connectors/{self.connector_id}/jobs/{job['job_id']}/result",
                    headers=self._headers(),
                    json={"lease_id": job["lease_id"], "result": result},
                )
                response.raise_for_status()
                return
            except httpx.HTTPError as exc:
                retryable = not isinstance(exc, httpx.HTTPStatusError) or (
                    exc.response.status_code in {408, 429, 500, 502, 503, 504}
                )
                if not retryable or attempt == 2:
                    raise
                await asyncio.sleep(2**attempt)

    async def submit_error(
        self,
        job: dict[str, Any],
        message: str,
        *,
        retryable: bool,
    ) -> None:
        response = await self.client.post(
            f"{self.root}/api/v1/connectors/{self.connector_id}/jobs/{job['job_id']}/error",
            headers=self._headers(),
            json={
                "lease_id": job["lease_id"],
                "message": message[:1000],
                "retryable": retryable,
            },
        )
        response.raise_for_status()


def _allowed_space(settings: Settings, value: str) -> str:
    cleaned = value.strip()
    if cleaned.casefold() not in {item.casefold() for item in settings.allowed_spaces}:
        raise ValueError("Job requested a space outside the local allowlist")
    return cleaned


async def execute_job(
    settings: Settings,
    confluence: ConfluenceClient,
    job: dict[str, Any],
) -> dict[str, Any]:
    job_type = job.get("type")
    payload = job.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Job payload is invalid")
    if job_type == "LIST_SPACES":
        if payload:
            raise ValueError("LIST_SPACES does not accept parameters")
        return {"spaces": await confluence.list_spaces()}
    if job_type == "LIST_CHILDREN":
        space_key = _allowed_space(settings, str(payload.get("space_key") or ""))
        parent = payload.get("parent_id")
        if parent is not None and (not isinstance(parent, str) or not PAGE_ID_RE.fullmatch(parent)):
            raise ValueError("Job contains an invalid parent page id")
        return {"pages": await confluence.list_children(space_key, parent)}
    if job_type == "LIST_SPACE_PAGES":
        space_key = _allowed_space(settings, str(payload.get("space_key") or ""))
        return await confluence.list_space_pages(space_key, payload.get("start", 0))
    if job_type == "IMPORT_PAGES":
        page_ids = payload.get("page_ids")
        page_spaces = payload.get("page_spaces")
        if (
            not isinstance(page_ids, list)
            or not isinstance(page_spaces, dict)
            or not page_ids
            or len(page_ids) > 100
            or any(not isinstance(item, str) or not PAGE_ID_RE.fullmatch(item) for item in page_ids)
            or set(page_spaces) != set(page_ids)
        ):
            raise ValueError("IMPORT_PAGES payload is invalid")
        pages: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for page_id in page_ids:
            space_key = _allowed_space(settings, str(page_spaces[page_id]))
            try:
                pages.append(await confluence.get_page(page_id, space_key))
            except ConfluenceError as exc:
                errors.append({"page_id": page_id, "message": str(exc)})
        return {"pages": pages, "errors": errors}
    raise ValueError("RAG requested an unsupported connector operation")


async def _heartbeat_loop(rag: RagClient, job: dict[str, Any], stopped: asyncio.Event) -> None:
    while not stopped.is_set():
        try:
            await asyncio.wait_for(stopped.wait(), timeout=rag.heartbeat_seconds)
        except TimeoutError:
            try:
                await rag.heartbeat(job)
            except Exception:
                logger.warning("Failed to extend connector job lease", exc_info=True)


async def run(settings: Settings, *, enroll_pilot: bool = False) -> None:
    rag = RagClient(settings)
    confluence = ConfluenceClient(settings)
    try:
        await rag.enroll(pilot=enroll_pilot)
        try:
            await confluence.check_connection()
            logger.info("Confluence connection verified")
        except ConfluenceError:
            logger.warning("Confluence is not currently available", exc_info=True)

        failures = 0
        while True:
            try:
                await rag.heartbeat()
                job = await rag.next_job()
                failures = 0
                if job is None:
                    continue
                logger.info("Received %s job %s", job.get("type"), job.get("job_id"))
                stopped = asyncio.Event()
                heartbeat = asyncio.create_task(_heartbeat_loop(rag, job, stopped))
                try:
                    result = await execute_job(settings, confluence, job)
                    await rag.submit_result(job, result)
                    logger.info("Completed connector job %s", job.get("job_id"))
                except (ConfluenceError, ValueError) as exc:
                    await rag.submit_error(
                        job,
                        str(exc),
                        retryable=isinstance(exc, ConfluenceError) and exc.retryable,
                    )
                    logger.warning("Connector job %s failed: %s", job.get("job_id"), exc)
                finally:
                    stopped.set()
                    heartbeat.cancel()
                    with suppress(asyncio.CancelledError):
                        await heartbeat
            except (httpx.HTTPError, RuntimeError) as exc:
                failures += 1
                delay = min(30, 2 ** min(failures, 5))
                logger.warning("RAG connection failed; retrying in %s seconds: %s", delay, exc)
                await asyncio.sleep(delay)
    finally:
        await confluence.close()
        await rag.close()
