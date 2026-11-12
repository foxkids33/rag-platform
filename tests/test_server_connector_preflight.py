import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "server_connector_preflight", ROOT / "scripts/deploy/server_connector_preflight.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_owner_only_pat_and_machine_file_permissions(tmp_path):
    credential = tmp_path / "credential"
    credential.write_text("synthetic secret")
    credential.chmod(0o400)
    MODULE.private_file(credential)
    with pytest.raises(ValueError):
        MODULE.private_file(credential, writable=True)
    credential.chmod(0o600)
    MODULE.private_file(credential, writable=True)
    credential.chmod(0o644)
    with pytest.raises(ValueError):
        MODULE.private_file(credential)


def test_no_symlink_or_hardlink_secret(tmp_path):
    credential = tmp_path / "credential"
    credential.write_text("synthetic secret")
    credential.chmod(0o600)
    symlink = tmp_path / "symlink"
    symlink.symlink_to(credential)
    with pytest.raises(ValueError):
        MODULE.private_file(symlink)
    hardlink = tmp_path / "hardlink"
    hardlink.hardlink_to(credential)
    with pytest.raises(ValueError):
        MODULE.private_file(credential)


@pytest.mark.parametrize("size", [0, 16_385])
def test_empty_and_oversize_secret_is_rejected(tmp_path, size):
    credential = tmp_path / "credential"
    credential.write_bytes(b"x" * size)
    credential.chmod(0o600)
    with pytest.raises(ValueError):
        MODULE.private_file(credential)
