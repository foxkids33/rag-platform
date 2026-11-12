import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.core.security import Principal
from app.services.upload_quota import check_upload_quota, upload_limit_bytes


class Counts:
    def __init__(self, values):
        self.values = iter(values)
        self.locked = False

    async def execute(self, *_args):
        self.locked = True

    async def scalar(self, _query):
        return next(self.values)


@pytest.mark.asyncio
@pytest.mark.parametrize("counts,expected", [([100], 413), ([0, 512 * 1024 * 1024], 413), ([0, 0, 2048 * 1024 * 1024], 507), ([0, 0, 0], None)])
async def test_private_workspace_and_kb_uploads_share_bounded_quota(monkeypatch, counts, expected):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_mode", "password")
    principal = Principal("alice", "local", frozenset(), {"auth_mode": "password"})
    for workspace_id, version_id in [("workspace", None), (None, "version")]:
        db = Counts(counts)
        if expected:
            with pytest.raises(HTTPException) as error:
                await check_upload_quota(db, principal, workspace_id, 1, version_id=version_id)
            assert error.value.status_code == expected
        else:
            await check_upload_quota(db, principal, workspace_id, 1, version_id=version_id)
        assert db.locked


def test_private_image_limits_are_smaller_and_admin_keeps_existing_limit(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_mode", "password")
    alice = Principal("alice", "local", frozenset(), {})
    admin = Principal("operator", "local", frozenset({settings.auth_admin_role}), {})
    assert upload_limit_bytes(alice, ".png") == 10 * 1024 * 1024
    assert upload_limit_bytes(alice, ".pdf") == 25 * 1024 * 1024
    assert upload_limit_bytes(admin, ".pdf") == settings.upload_max_bytes
