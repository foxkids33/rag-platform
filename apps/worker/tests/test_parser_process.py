from __future__ import annotations

import subprocess

import pytest
from app import parser_process


def test_parser_process_preserves_unicode_and_chunk_identity():
    chunks = parser_process.parse_document_isolated(
        "file.txt", "Пример документа BLUE-KB-942".encode()
    )
    assert "Пример документа BLUE-KB-942" in chunks[0].text
    assert chunks[0].parent_chunk_id


def test_parser_process_reports_safe_validation_error():
    with pytest.raises(ValueError, match="PDF signature is invalid"):
        parser_process.parse_document_isolated("file.pdf", b"not a PDF")


def test_parser_process_timeout_kills_the_group(monkeypatch):
    calls = []

    class Process:
        pid = 123456
        returncode = -9

        def __init__(self, *args, **kwargs):
            assert kwargs["start_new_session"] is True

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def communicate(self, input=None, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("parser", timeout)
            return b"", b""

    monkeypatch.setattr(parser_process.subprocess, "Popen", Process)
    monkeypatch.setattr(parser_process.os, "killpg", lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="time limit"):
        parser_process.parse_document_isolated("file.txt", b"text")
    assert calls[0][0] == 123456


def test_parser_process_crash_is_not_success(monkeypatch):
    class Process:
        returncode = -11

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def communicate(self, **kwargs):
            return b"", b"decoder native error, not safe to disclose"

    monkeypatch.setattr(parser_process.subprocess, "Popen", Process)
    with pytest.raises(ValueError, match="resource limit or stopped"):
        parser_process.parse_document_isolated("file.pdf", b"%PDF-")
