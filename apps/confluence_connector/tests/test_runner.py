import argparse
import asyncio
import json

import httpx
import pytest

from confluence_connector.__main__ import discover_accessible_spaces, load_settings
from confluence_connector.config import Settings, normalize_rag_url
from confluence_connector.confluence import ConfluenceClient
from confluence_connector.runner import RagClient


def config(tmp_path):
    return Settings(
        _env_file=None,
        rag_url="https://rag.example.test",
        confluence_url="https://wiki.example.test",
        confluence_token="test",
        rag_credential_file=tmp_path / "credential.json",
    )


def test_result_is_retried_verbatim_after_lost_ack(tmp_path, monkeypatch):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            raise httpx.ReadTimeout("Lost response")
        return httpx.Response(200, json={"status": "SUCCEEDED"})

    async def no_wait(_):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_wait)

    async def run():
        rag = RagClient(config(tmp_path))
        await rag.client.aclose()
        rag.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        rag.connector_id, rag.credential = "connector", "test"
        try:
            await rag.submit_result({"job_id": "job", "lease_id": "lease"}, {"pages": []})
        finally:
            await rag.close()

    asyncio.run(run())
    assert len(requests) == 2 and requests[0] == requests[1]


def test_saved_credential_is_private_and_bound_to_destination(tmp_path):
    async def run():
        rag = RagClient(config(tmp_path))
        try:
            rag._save_credential("connector", "test")
            assert rag.settings.credential_path.stat().st_mode & 0o777 == 0o600
            rag.root = "https://other.example.test"
            with pytest.raises(RuntimeError, match="another RAG_URL"):
                rag._load_credential()
        finally:
            await rag.close()

    asyncio.run(run())


def test_project_env_fallback_preserves_explicit_connector_settings(tmp_path):
    project = tmp_path / "project.env"
    project.write_text(
        "CONFLUENCE_BASE_URL=https://wiki.example.test\nCONFLUENCE_AUTH_MODE=basic\nCONFLUENCE_EMAIL=user\nCONFLUENCE_API_TOKEN=local-secret\n"
    )
    connector = tmp_path / "connector.env"
    connector.write_text("CONFLUENCE_AUTH_MODE=bearer\nCONFLUENCE_TOKEN=connector-secret\n")
    settings = load_settings(
        argparse.Namespace(
            env_file=str(connector),
            project_env=str(project),
            rag_url="http://rag.example.test:15173",
            allow_http=True,
        )
    )
    assert settings.confluence_auth_mode == "bearer"
    assert settings.confluence_token.get_secret_value() == "connector-secret"
    assert settings.confluence_url == "https://wiki.example.test"
    connector.write_text("CONFLUENCE_AUTH_MODE=bearer\nCONFLUENCE_TOKEN_FILE=/tmp/local-token\n")
    settings = load_settings(
        argparse.Namespace(
            env_file=str(connector),
            project_env=str(project),
            rag_url="http://rag.example.test:15173",
            allow_http=True,
        )
    )
    assert settings.confluence_token.get_secret_value() == ""
    assert str(settings.confluence_token_file) == "/tmp/local-token"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" HTTPS://RAG.Example.Test:443/ ", "https://rag.example.test"),
        ("http://192.0.2.122:15173/", "http://192.0.2.122:15173"),
        ("https://rag.example.test:8443/Api/", "https://rag.example.test:8443/Api"),
        ("http://[::1]:80/", "http://[::1]"),
    ],
)
def test_destination_normalization(value, expected):
    assert normalize_rag_url(value) == expected


@pytest.mark.parametrize("value", [
    "https://user:secret@rag.example.test", "https://rag.example.test?query=test",
    "https://rag.example.test#fragment", "https://rag.example.test:0", "not a URL",
])
def test_destination_normalization_rejects_ambiguous_binding(value):
    with pytest.raises(ValueError):
        normalize_rag_url(value)


def test_default_credentials_are_scoped_to_normalized_destination(tmp_path, monkeypatch):
    monkeypatch.delenv("RAG_CREDENTIAL_FILE", raising=False)
    monkeypatch.setattr("confluence_connector.config.DEFAULT_CREDENTIAL_DIRECTORY", tmp_path)
    settings = config(tmp_path).model_copy(update={"rag_credential_file": None})
    equivalent = settings.model_copy(update={"rag_url": "https://RAG.example.test:443/"})
    different = settings.model_copy(update={"rag_url": "https://other.example.test"})
    other_path = settings.model_copy(update={"rag_url": "https://rag.example.test/api"})
    assert settings.credential_path == equivalent.credential_path
    paths = {settings.credential_path, different.credential_path, other_path.credential_path}
    assert len(paths) == 3
    assert settings.credential_path.parent == tmp_path


def test_exact_user_command_can_enroll_new_target_without_overwriting_old(tmp_path, monkeypatch):
    monkeypatch.delenv("RAG_CREDENTIAL_FILE", raising=False)
    monkeypatch.setattr("confluence_connector.config.DEFAULT_CREDENTIAL_DIRECTORY", tmp_path)
    project = tmp_path / "project.env"
    project.write_text(
        "CONFLUENCE_BASE_URL=https://wiki.example.test\n"
        "CONFLUENCE_AUTH_MODE=bearer\nCONFLUENCE_API_TOKEN=confluence-test-token\n"
    )
    old = tmp_path / "credential.json"
    old_bytes = json.dumps({
        "connector_id": "old-connector", "credential": "old-test-credential",
        "rag_url": "http://192.0.2.60:15173",
    }).encode()
    old.write_bytes(old_bytes)
    settings = load_settings(argparse.Namespace(
        env_file=str(tmp_path / "missing.env.connector"), project_env=str(project),
        rag_url="http://192.0.2.122:15173", allow_http=True,
    ))
    requests = []

    def handler(request):
        requests.append(request)
        assert "Authorization" not in request.headers
        if request.url.path.endswith("/enrollments"):
            return httpx.Response(201, json={"enrollment_token": "new-enrollment"})
        return httpx.Response(200, json={
            "connector_id": "new-connector", "credential": "new-test-credential",
        })

    async def run():
        rag = RagClient(settings)
        await rag.client.aclose()
        rag.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await rag.enroll(pilot=True)
            assert rag.connector_id == "new-connector"
            assert settings.credential_path != old
            assert settings.credential_path.stat().st_mode & 0o777 == 0o600
            assert rag._load_credential()
        finally:
            await rag.close()

    asyncio.run(run())
    assert len(requests) == 2
    assert old.read_bytes() == old_bytes


def test_matching_legacy_credential_is_copied_not_removed(tmp_path, monkeypatch):
    monkeypatch.setattr("confluence_connector.config.DEFAULT_CREDENTIAL_DIRECTORY", tmp_path)
    settings = config(tmp_path).model_copy(update={"rag_credential_file": None})
    legacy = settings.legacy_credential_path
    legacy.write_text(json.dumps({
        "connector_id": "old-connector", "credential": "old-credential",
        "rag_url": "https://RAG.example.test:443/",
    }))
    before = legacy.read_bytes()

    async def run():
        rag = RagClient(settings)
        try:
            assert rag._load_credential()
            assert (rag.connector_id, rag.credential) == ("old-connector", "old-credential")
            assert settings.credential_path.exists()
            assert settings.credential_path.stat().st_mode & 0o777 == 0o600
        finally:
            await rag.close()

    asyncio.run(run())
    assert legacy.read_bytes() == before


@pytest.mark.parametrize("legacy_data", ["[]", "not json", '{"credential":"test"}'])
def test_invalid_or_unbound_legacy_credentials_are_not_reused(tmp_path, monkeypatch, legacy_data):
    monkeypatch.setattr("confluence_connector.config.DEFAULT_CREDENTIAL_DIRECTORY", tmp_path)
    settings = config(tmp_path).model_copy(update={"rag_credential_file": None})
    settings.legacy_credential_path.write_text(legacy_data)

    async def run():
        rag = RagClient(settings)
        try:
            assert not rag._load_credential()
            assert rag.credential is None
            assert not settings.credential_path.exists()
        finally:
            await rag.close()

    asyncio.run(run())


def test_explicit_credentials_file_wins_over_environment_and_stays_strict(tmp_path, monkeypatch):
    monkeypatch.setenv("RAG_CREDENTIAL_FILE", str(tmp_path / "env-credential.json"))
    project = tmp_path / "project.env"
    project.write_text(
        "CONFLUENCE_BASE_URL=https://wiki.example.test\nCONFLUENCE_API_TOKEN=test\n"
    )
    explicit = tmp_path / "explicit.json"
    explicit.write_text(json.dumps({
        "connector_id": "old", "credential": "test", "rag_url": "https://other.example.test",
    }))
    settings = load_settings(argparse.Namespace(
        env_file=str(tmp_path / "missing.env.connector"), project_env=str(project),
        rag_url="https://rag.example.test", allow_http=False, credentials_file=explicit,
    ))
    assert settings.credential_path == explicit

    async def run():
        rag = RagClient(settings)
        try:
            with pytest.raises(RuntimeError, match="another RAG_URL"):
                await rag.enroll(pilot=True)
        finally:
            await rag.close()

    asyncio.run(run())


def test_discovery_uses_visible_spaces_as_actual_allowlist(monkeypatch, tmp_path):
    settings = config(tmp_path)

    async def visible(self, *, include_all_accessible=False):
        assert include_all_accessible
        return [{"key": "KBR"}, {"key": "OTHER"}]

    monkeypatch.setattr(ConfluenceClient, "list_spaces", visible)
    discovered = asyncio.run(discover_accessible_spaces(settings))
    assert discovered.allowed_spaces == ("KBR", "OTHER")
    assert settings.allowed_spaces == ("KBR",)
    assert discovered.credential_path == settings.credential_path
