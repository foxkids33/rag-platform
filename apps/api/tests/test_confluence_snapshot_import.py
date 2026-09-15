from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.operations.import_confluence_snapshot import (
    SnapshotValidationError,
    validate_snapshot,
)

SOURCE = "https://confluence.example.test"


def write_snapshot(root: Path, *, checksum: str | None = None, page_path: str = "run/123.json") -> None:
    page = {
        "id": "123",
        "type": "page",
        "status": "current",
        "title": "Release page",
        "space": {"key": "KBR"},
        "version": {"number": 7},
        "ancestors": [{"title": "Root"}],
        "metadata": {"labels": {"results": [{"name": "release"}]}},
        "body": {"storage": {"value": "<p>Frozen content</p>"}},
        "_links": {"base": SOURCE, "webui": "/spaces/KBR/pages/123"},
    }
    raw = json.dumps(page, ensure_ascii=False).encode()
    target = root / "run" / "123.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(raw)
    manifest = {
        "run_id": "frozen-1",
        "source": SOURCE,
        "space": "KBR",
        "inventory_complete": True,
        "snapshot_complete": True,
        "pages": [{
            "id": "123",
            "title": "Release page",
            "version": "7",
            "path": page_path,
            "sha256": checksum or hashlib.sha256(raw).hexdigest(),
        }],
    }
    (root / "latest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_validates_complete_snapshot(tmp_path: Path) -> None:
    write_snapshot(tmp_path)
    snapshot = validate_snapshot(
        tmp_path,
        expected_source=SOURCE,
        expected_space="KBR",
        expected_pages=1,
    )
    assert snapshot.run_id == "frozen-1"
    assert snapshot.pages[0].storage_html == "<p>Frozen content</p>"
    assert snapshot.pages[0].source_url == f"{SOURCE}/spaces/KBR/pages/123"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"checksum": "0" * 64}, "checksum mismatch"),
        ({"page_path": "../outside.json"}, "escapes or is missing"),
    ],
)
def test_rejects_untrusted_page_files(tmp_path: Path, change: dict[str, str], message: str) -> None:
    write_snapshot(tmp_path, **change)
    with pytest.raises(SnapshotValidationError, match=message):
        validate_snapshot(
            tmp_path,
            expected_source=SOURCE,
            expected_space="KBR",
            expected_pages=1,
        )


def test_rejects_wrong_source_host(tmp_path: Path) -> None:
    write_snapshot(tmp_path)
    with pytest.raises(SnapshotValidationError, match="source host"):
        validate_snapshot(
            tmp_path,
            expected_source="https://other.example.test",
            expected_space="KBR",
            expected_pages=1,
        )
