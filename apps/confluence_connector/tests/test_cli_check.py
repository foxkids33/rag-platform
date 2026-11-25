import asyncio

import httpx
import pytest

from confluence_connector import __main__ as cli
from confluence_connector.config import Settings
from confluence_connector.runner import RagClient


class FakeConfluence:
    closed = False

    async def check_connection(self):
        return None

    async def list_spaces(self):
        return [{"key": "KBR"}]

    async def close(self):
        self.closed = True


def config(tmp_path):
    return Settings(_env_file=None, rag_url="https://rag.example.test",
                    confluence_url="https://wiki.example.test", confluence_token="test-token",
                    rag_credential_file=tmp_path / "credential.json")


@pytest.mark.parametrize("protected_status", [401, 403])
def test_protected_user_api_check_uses_machine_heartbeat_not_job_poll(
    tmp_path, monkeypatch, capsys, protected_status,
):
    requests = []
    fake_confluence = FakeConfluence()

    def handler(request):
        requests.append(request)
        if request.url.path == "/api/v1/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/v1/integrations/confluence/connector":
            assert "Authorization" not in request.headers
            return httpx.Response(protected_status)
        if request.url.path == "/api/v1/connectors/test-connector/heartbeat":
            assert request.method == "POST"
            assert request.headers["Authorization"] == "Bearer machine-test-secret"
            return httpx.Response(204)
        raise AssertionError("Unexpected endpoint: no enrollment or job may be consumed")

    async def run():
        rag = RagClient(config(tmp_path))
        await rag.client.aclose()
        rag.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        rag._save_credential("test-connector", "machine-test-secret")
        saved = rag.settings.credential_path.read_bytes()
        monkeypatch.setattr(cli, "RagClient", lambda _settings: rag)
        monkeypatch.setattr(cli, "ConfluenceClient", lambda _settings: fake_confluence)
        await cli.check(rag.settings)
        assert rag.settings.credential_path.read_bytes() == saved
        assert rag.client.is_closed and fake_confluence.closed

    asyncio.run(run())
    assert len(requests) == 3
    assert "RAG connector machine API: OK (authenticated heartbeat)" in capsys.readouterr().out


def test_protected_user_api_without_machine_credential_explains_enrollment(tmp_path, monkeypatch):
    requests = []
    fake_confluence = FakeConfluence()

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(401)

    async def run():
        rag = RagClient(config(tmp_path))
        await rag.client.aclose()
        rag.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(cli, "RagClient", lambda _settings: rag)
        monkeypatch.setattr(cli, "ConfluenceClient", lambda _settings: fake_confluence)
        with pytest.raises(RuntimeError, match="enroll this connector.*first"):
            await cli.check(rag.settings)
        assert rag.client.is_closed and fake_confluence.closed

    asyncio.run(run())
    assert len(requests) == 2


def test_unauthenticated_pilot_check_preserves_existing_enabled_api_path(
    tmp_path, monkeypatch, capsys,
):
    fake_confluence = FakeConfluence()
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        return httpx.Response(200, json={"enabled": True})

    async def run():
        rag = RagClient(config(tmp_path))
        await rag.client.aclose()
        rag.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(cli, "RagClient", lambda _settings: rag)
        monkeypatch.setattr(cli, "ConfluenceClient", lambda _settings: fake_confluence)
        await cli.check(rag.settings)
        assert not rag.settings.credential_path.exists()

    asyncio.run(run())
    assert len(requests) == 2
    assert "RAG connector API: OK" in capsys.readouterr().out


def test_machine_credential_rejection_is_not_reported_as_healthy(tmp_path, monkeypatch, capsys):
    fake_confluence = FakeConfluence()

    def handler(request):
        if request.url.path.endswith("/health"):
            return httpx.Response(200)
        return httpx.Response(401)

    async def run():
        rag = RagClient(config(tmp_path))
        await rag.client.aclose()
        rag.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        rag._save_credential("test-connector", "revoked-test-secret")
        monkeypatch.setattr(cli, "RagClient", lambda _settings: rag)
        monkeypatch.setattr(cli, "ConfluenceClient", lambda _settings: fake_confluence)
        with pytest.raises(httpx.HTTPStatusError):
            await cli.check(rag.settings)
        assert rag.client.is_closed and fake_confluence.closed

    asyncio.run(run())
    assert "RAG connector machine API: OK" not in capsys.readouterr().out
