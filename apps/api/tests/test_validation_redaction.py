import httpx
import pytest

from app.main import app


@pytest.mark.asyncio
@pytest.mark.parametrize("password", ["s3cret", "secret-prefix-" + "x" * 130])
async def test_invalid_auth_password_is_never_reflected(password):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/auth/register", json={"username": "sample", "password": password})
    assert response.status_code == 422
    assert password not in response.text
    assert all("input" not in error and "ctx" not in error for error in response.json()["detail"])
