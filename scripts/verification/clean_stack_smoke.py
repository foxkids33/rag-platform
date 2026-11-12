"""Integration smoke test for a disposable synthetic clean-install stack.

Refuses non-loopback URLs, real model names, and nonempty workspaces. Never
use this script against a working installation. Fixtures are not model evals.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def local_url(value):
    parts = urlsplit(value)
    if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost"} or parts.username or parts.password:
        raise argparse.ArgumentTypeError("Use the loopback HTTP address of the disposable fixture stack")
    return value.rstrip("/")


def request(base, path, payload=None, method=None, *, expected=200, raw=False):
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(base + path, data=data, method=method or ("POST" if data is not None else "GET"),
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=60) as response:
            code, body = response.status, response.read()
    except HTTPError as error:
        code, body = error.code, error.read()
    if code != expected:
        raise AssertionError(f"{req.method} {path}: expected {expected}, got {code}: {body[:500]!r}")
    return body.decode() if raw else json.loads(body) if body else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", type=local_url, default="http://127.0.0.1:18573")
    parser.add_argument("--fixtures-url", type=local_url, default="http://127.0.0.1:18574")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--confirm-synthetic", action="store_true", required=True)
    args = parser.parse_args()
    api = args.base_url + "/api/v1"
    assert request(args.fixtures_url, "/health")["synthetic"] is True
    assert request(api, "/system/info")["generation_model"] == "clean-test-model"
    assert request(api, "/workspaces") == [], "Refusing to modify a nonempty installation"
    assert request(api, "/knowledge-bases") == [], "Expected a clean knowledge-base list"
    checks = ["fresh database has no workspaces or knowledge bases"]
    connection = request(api, "/integrations/confluence/connection")
    assert connection["mode"] == "direct" and connection["ready"]
    assert connection["allowed_spaces"] == ["TEAM"] and connection.get("connector") is None
    assert "synthetic-test-only" not in json.dumps(connection)
    workspace = request(api, "/workspaces", {"name": "Synthetic smoke test", "source_mode": "USER_DOCUMENTS"}, expected=201)
    prefix = f"/workspaces/{workspace['id']}"
    direct = prefix + "/integrations/confluence/direct"
    spaces = request(api, direct + "/spaces", {})
    assert [space["key"] for space in spaces["spaces"]] == ["TEAM"]
    request(api, direct + "/children", {"space_key": "PRIVATE"}, expected=403)
    forged = request(api, direct + "/imports", {"page_ids": ["999"], "page_spaces": {"999": "TEAM"}}, expected=202)
    assert forged["failed"] == 1 and not request(api, prefix + "/documents")
    checks.append("direct capability, secret exclusion, and actual page-space allowlist")
    payload = {"page_ids": ["101"], "page_spaces": {"101": "TEAM"}, "expected_versions": {"101": "1"}}
    imported = request(api, direct + "/imports", payload, expected=202)
    assert imported["queued"] == 1 and not imported["failed"]
    print("Imported synthetic Confluence page; waiting for worker", flush=True)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        documents = request(api, prefix + "/documents")
        assert len(documents) == 1
        if documents[0]["status"] == "READY":
            break
        if documents[0]["status"] == "FAILED":
            raise AssertionError("Synthetic document ingestion failed; inspect only disposable-stack logs")
        time.sleep(1)
    else:
        raise AssertionError("Timed out waiting for synthetic ingestion")
    repeated = request(api, direct + "/imports", payload, expected=202)
    assert repeated["skipped"] == 1 and not repeated["queued"]
    assert len(request(api, prefix + "/documents")) == 1
    checks.append("Confluence -> object store -> database job -> worker -> embeddings; idempotent reimport")
    question = "Как восстановить тестовый сервис?"
    result = request(api, prefix + "/search", {"query": question, "mode": "hybrid", "rerank": False})
    assert result["results"] and result["results"][0]["source_page_id"] == "101"
    answer = request(api, prefix + "/answer", {"question": question, "rerank": False, "max_tokens": 256})
    assert not answer["abstained"] and answer["citation_valid"] and answer["sources"]
    assert 1 in answer["cited_source_indices"]
    stream = request(api, prefix + "/answer/stream", {"question": question, "rerank": False, "max_tokens": 256}, raw=True)
    assert "event: done" in stream and "event: error" not in stream
    checks.append("hybrid search, OpenAI-compatible fixture chat, valid source references and SSE")
    document_id = documents[0]["id"]
    request(api, prefix + "/documents/" + document_id, {"search_enabled": False}, method="PATCH")
    excluded = request(api, prefix + "/search", {"query": question, "rerank": False})
    assert not excluded["results"]
    request(api, prefix + "/documents/" + document_id, {"search_enabled": True}, method="PATCH")
    request(api, prefix + "/documents/" + document_id, method="DELETE", expected=204)
    assert not request(api, prefix + "/documents")
    checks.append("exclude from search, re-enable, delete synthetic document")
    counts = request(args.fixtures_url, "/__fixture__/stats")["counts"]
    assert all(counts[key] > 0 for key in ("confluence", "embedding", "chat"))
    report = {"synthetic": True, "model_quality_evaluation": False, "checks": checks,
              "fixture_requests": counts, "workspace_id": workspace["id"]}
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
