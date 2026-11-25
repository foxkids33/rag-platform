import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[1] / "deploy/delivery/poll.py"
spec = importlib.util.spec_from_file_location("rag_delivery", path)
delivery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(delivery)


def configure(monkeypatch, state, changed):
    monkeypatch.setattr(delivery, "STATE", state)
    delivery.save(state / "applied.json", "previous")
    monkeypatch.setattr(delivery, "get_target", lambda: "new")
    monkeypatch.setattr(delivery, "git", lambda *args: changed if args[0] == "diff" else "")
    monkeypatch.setattr(delivery, "current", lambda s: {"alias": s, "old": "old-image"})
    monkeypatch.setattr(delivery, "build", lambda *args: "new-image")


def read(state, file):
    return json.loads((state / file).read_text())


def test_docs_only_advances_without_restarting_or_building(tmp_path, monkeypatch):
    configure(monkeypatch, tmp_path, "docs/delivery.md")
    monkeypatch.setattr(delivery, "build", lambda *args: pytest.fail("unexpected build"))
    monkeypatch.setattr(delivery, "switch", lambda *args: pytest.fail("unexpected restart"))
    delivery.main()
    assert read(tmp_path, "applied.json") == "new"


def test_failed_release_restores_last_images_and_does_not_loop(tmp_path, monkeypatch):
    configure(monkeypatch, tmp_path, "apps/worker/app/main.py")
    calls = []
    def switch(images, key):
        assert list(images) == ["worker"]
        calls.append(key)
        if key == "new":
            raise RuntimeError("worker readiness")
    monkeypatch.setattr(delivery, "switch", switch)
    with pytest.raises(RuntimeError, match="worker readiness"):
        delivery.main()
    delivery.main()
    assert calls == ["new", "old"]
    assert read(tmp_path, "applied.json") == "previous"
    assert read(tmp_path, "failed.json") == "new"
    assert not (tmp_path / "pending.json").exists()


def test_build_failure_leaves_running_version_available_for_retry(tmp_path, monkeypatch):
    configure(monkeypatch, tmp_path, "apps/web/src/App.tsx")
    def failed_build(*args):
        raise subprocess.CalledProcessError(1, "docker build")
    monkeypatch.setattr(delivery, "build", failed_build)
    with pytest.raises(subprocess.CalledProcessError):
        delivery.main()
    assert read(tmp_path, "applied.json") == "previous"
    assert not (tmp_path / "pending.json").exists()
    assert not (tmp_path / "failed.json").exists()
