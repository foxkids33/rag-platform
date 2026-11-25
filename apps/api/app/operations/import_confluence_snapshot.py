from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from sqlalchemy import select

from app.api.routes.confluence import _import_page
from app.db.models import Workspace
from app.db.session import SessionLocal
from app.services.confluence import ConfluencePage


class SnapshotValidationError(ValueError):
    """The frozen snapshot cannot be trusted for import."""


@dataclass(frozen=True, slots=True)
class ValidatedSnapshot:
    source: str
    space: str
    run_id: str
    pages: tuple[ConfluencePage, ...]


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, (str, int)):
        raise SnapshotValidationError(f"{field} is missing or invalid")
    result = str(value).strip()
    if not result:
        raise SnapshotValidationError(f"{field} is empty")
    return result


def _origin(value: str, field: str) -> tuple[str, str]:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SnapshotValidationError(f"{field} must be an absolute HTTP(S) URL")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, f"{parsed.hostname.casefold()}:{port}"


def _load_json(path: Path, field: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SnapshotValidationError(f"cannot read {field}: {path}") from exc
    if not isinstance(value, dict):
        raise SnapshotValidationError(f"{field} must contain a JSON object")
    return value


def _safe_page_path(root: Path, relative_path: Any) -> Path:
    relative = Path(_required_text(relative_path, "page.path"))
    if relative.is_absolute():
        raise SnapshotValidationError("page.path must be relative to the snapshot root")
    try:
        candidate = (root / relative).resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError) as exc:
        raise SnapshotValidationError(f"page.path escapes or is missing: {relative}") from exc
    if not candidate.is_file():
        raise SnapshotValidationError(f"page.path is not a file: {relative}")
    return candidate


def _page_from_payload(
    payload: dict[str, Any],
    *,
    expected_id: str,
    expected_title: str,
    expected_version: str,
    expected_space: str,
    source: str,
) -> ConfluencePage:
    page_id = _required_text(payload.get("id"), "page.id")
    title = _required_text(payload.get("title"), "page.title")
    version_data = payload.get("version")
    version = _required_text(
        version_data.get("number") if isinstance(version_data, dict) else None,
        "page.version.number",
    )
    space_data = payload.get("space")
    space = _required_text(
        space_data.get("key") if isinstance(space_data, dict) else None,
        "page.space.key",
    )
    storage = (payload.get("body") or {}).get("storage")
    storage_html = storage.get("value") if isinstance(storage, dict) else None
    if not isinstance(storage_html, str):
        raise SnapshotValidationError(f"page {page_id} has no Confluence storage body")
    if payload.get("status") != "current":
        raise SnapshotValidationError(f"page {page_id} is not current")
    if (page_id, title, version, space) != (
        expected_id,
        expected_title,
        expected_version,
        expected_space,
    ):
        raise SnapshotValidationError(f"page {page_id} metadata differs from the manifest")

    links = payload.get("_links") or {}
    base = links.get("base") if isinstance(links, dict) else None
    web_ui = links.get("webui") if isinstance(links, dict) else None
    if base and _origin(str(base), "page._links.base") != _origin(source, "manifest.source"):
        raise SnapshotValidationError(f"page {page_id} points to a different Confluence host")
    source_url = urljoin(f"{source.rstrip('/')}/", str(web_ui).lstrip("/")) if web_ui else None
    if source_url and _origin(source_url, "page source URL") != _origin(source, "manifest.source"):
        raise SnapshotValidationError(f"page {page_id} source URL leaves the expected host")

    ancestors = payload.get("ancestors") or []
    labels = ((payload.get("metadata") or {}).get("labels") or {}).get("results") or []
    return ConfluencePage(
        id=page_id,
        title=title,
        storage_html=storage_html,
        version=version,
        source_url=source_url,
        space_key=space,
        ancestors=[str(item["title"]) for item in ancestors if isinstance(item, dict) and item.get("title")],
        labels=[str(item["name"]) for item in labels if isinstance(item, dict) and item.get("name")],
    )


def validate_snapshot(
    snapshot_root: Path,
    *,
    expected_source: str,
    expected_space: str,
    expected_pages: int,
) -> ValidatedSnapshot:
    root = snapshot_root.resolve(strict=True)
    if not root.is_dir():
        raise SnapshotValidationError("snapshot root is not a directory")
    manifest = _load_json(root / "latest.json", "latest.json")
    source = _required_text(manifest.get("source"), "manifest.source").rstrip("/")
    space = _required_text(manifest.get("space"), "manifest.space")
    run_id = _required_text(manifest.get("run_id"), "manifest.run_id")
    if not manifest.get("inventory_complete") or not manifest.get("snapshot_complete"):
        raise SnapshotValidationError("manifest is not marked inventory_complete and snapshot_complete")
    if _origin(source, "manifest.source") != _origin(expected_source, "expected source"):
        raise SnapshotValidationError("manifest source host differs from --expected-source")
    if space != expected_space:
        raise SnapshotValidationError("manifest space differs from --expected-space")

    records = manifest.get("pages")
    if not isinstance(records, list) or len(records) != expected_pages:
        found = len(records) if isinstance(records, list) else 0
        raise SnapshotValidationError(f"expected {expected_pages} pages, found {found}")

    pages: list[ConfluencePage] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise SnapshotValidationError("manifest.pages contains a non-object entry")
        page_id = _required_text(record.get("id"), "manifest page id")
        title = _required_text(record.get("title"), f"manifest page {page_id} title")
        version = _required_text(record.get("version"), f"manifest page {page_id} version")
        if page_id in seen:
            raise SnapshotValidationError(f"duplicate page id in manifest: {page_id}")
        seen.add(page_id)
        page_path = _safe_page_path(root, record.get("path"))
        expected_sha = _required_text(record.get("sha256"), f"manifest page {page_id} sha256")
        if len(expected_sha) != 64 or any(char not in "0123456789abcdef" for char in expected_sha.lower()):
            raise SnapshotValidationError(f"page {page_id} has an invalid SHA-256 value")
        raw = page_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_sha.lower():
            raise SnapshotValidationError(f"checksum mismatch for page {page_id}")
        try:
            payload = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SnapshotValidationError(f"page {page_id} contains invalid JSON") from exc
        if not isinstance(payload, dict):
            raise SnapshotValidationError(f"page {page_id} must contain a JSON object")
        pages.append(
            _page_from_payload(
                payload,
                expected_id=page_id,
                expected_title=title,
                expected_version=version,
                expected_space=expected_space,
                source=source,
            )
        )
    return ValidatedSnapshot(source=source, space=space, run_id=run_id, pages=tuple(pages))


async def import_snapshot(snapshot: ValidatedSnapshot, workspace_id: uuid.UUID, *, force: bool) -> int:
    async with SessionLocal() as db:
        workspace = await db.scalar(select(Workspace).where(Workspace.id == workspace_id))
        if workspace is None:
            raise SnapshotValidationError(f"target workspace does not exist: {workspace_id}")
        counts: dict[str, int] = {}
        for index, page in enumerate(snapshot.pages, start=1):
            item = await _import_page(db, workspace_id, page, force=force)
            counts[item.status] = counts.get(item.status, 0) + 1
            if index % 25 == 0 or index == len(snapshot.pages):
                print(f"progress={index}/{len(snapshot.pages)}")
        print("result=" + ",".join(f"{key}:{counts[key]}" for key in sorted(counts)))
        return counts.get("failed", 0)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and import a frozen Confluence snapshot")
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--workspace-id", type=uuid.UUID, required=True)
    parser.add_argument("--expected-source", required=True)
    parser.add_argument("--expected-space", default="KBR")
    parser.add_argument("--expected-pages", type=int, default=298)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Perform writes after validation")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        snapshot = validate_snapshot(
            args.snapshot_root,
            expected_source=args.expected_source,
            expected_space=args.expected_space,
            expected_pages=args.expected_pages,
        )
        print(
            f"validated run={snapshot.run_id} source={snapshot.source} "
            f"space={snapshot.space} pages={len(snapshot.pages)}"
        )
        if not args.apply:
            print("dry-run complete; add --apply to import the validated snapshot")
            return 0
        return 1 if asyncio.run(import_snapshot(snapshot, args.workspace_id, force=args.force)) else 0
    except (SnapshotValidationError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
