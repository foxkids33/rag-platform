import pytest

from app.document_processing import parse_document
from app.text_processing import extract_text


def test_markdown_yaml_fence_preserves_indentation_and_trailing_whitespace():
    block = "```yaml\nservice:\n  readiness:\n    timeout_seconds: 3  \n\n    enabled: true\n```\n"
    chunks = parse_document("runbook.md", ("Before\n\n" + block + "\nAfter\n").encode())

    code = [chunk for chunk in chunks if chunk.metadata.get("block_kind") == "code"]
    assert len(code) == 1
    assert code[0].text == block
    assert block in code[0].parent_text + "\n"
    assert "    timeout_seconds: 3  \n\n" in code[0].text


def test_markdown_crlf_only_normalizes_line_endings_inside_fences():
    block = "```yaml\r\nkey:\r\n\tchild:  two spaces\r\n\r\n```\r\n"
    chunks = parse_document("windows.md", block.encode())

    assert chunks[0].text == block.replace("\r\n", "\n")


def test_markdown_shell_block_stays_atomic_when_larger_than_chunk_limit(monkeypatch):
    from app import document_processing

    monkeypatch.setattr(document_processing.settings, "chunk_max_chars", 80)
    monkeypatch.setattr(document_processing.settings, "chunk_min_chars", 1)
    monkeypatch.setattr(document_processing.settings, "parent_max_chars", 100)
    command = 'curl --silent \\\n  --header "X:  two spaces" \\\n  http://127.0.0.1:18443/ready\n\n'
    block = "```bash\n" + command * 8 + "```\n"
    chunks = parse_document("commands.MD", ("Before\n\n" + block + "\nAfter").encode())

    containing = [chunk for chunk in chunks if block in chunk.text]
    assert len(containing) == 1
    assert containing[0].text == block
    assert containing[0].metadata["oversized_atomic_block"]
    assert command * 8 in containing[0].parent_text


def test_markdown_surrounding_prose_uses_existing_normalization():
    before = "  First   paragraph.\n\n\nSecond\t paragraph.\n\n"
    after = "\n\n  Last   paragraph.\n"
    block = "```yaml\nkey:\n  value: 1\n```\n"
    chunks = parse_document("runbook.md", (before + block + after).encode())

    prose = [chunk.text for chunk in chunks if chunk.metadata.get("block_kind") == "prose"]
    assert prose == [extract_text("runbook.md", before.encode()), extract_text("runbook.md", after.encode())]
    assert all(chunk.metadata["chunking"] == "parent_child_v1" for chunk in chunks)


def test_markdown_without_fences_keeps_plain_text_behavior():
    data = b"  First   paragraph.\n\n\nSecond\t paragraph.\n"
    markdown = parse_document("sample.md", data)
    plain = parse_document("sample.txt", data)

    assert [chunk.text for chunk in markdown] == [chunk.text for chunk in plain]
    assert [chunk.parent_text for chunk in markdown] == [chunk.parent_text for chunk in plain]
    assert all(chunk.metadata["parser"] == "plain_text_v2" for chunk in markdown)


@pytest.mark.parametrize("opening,closing", [("~~~~yaml", "~~~~~"), ("````text", "````")])
def test_markdown_fences_keep_shorter_markers_inside_code(opening, closing):
    block = f"{opening}\n  first\n```\n  last\n{closing}\n"
    chunks = parse_document("runbook.md", block.encode())

    assert len(chunks) == 1
    assert chunks[0].text == block
    assert chunks[0].metadata["warnings"] == []


def test_markdown_unclosed_code_is_atomic_and_explicitly_flagged(monkeypatch):
    from app import document_processing

    monkeypatch.setattr(document_processing.settings, "chunk_max_chars", 40)
    block = "```yaml\n" + "  value: true\n" * 12
    chunks = parse_document("incomplete.md", block.encode())

    assert len(chunks) == 1
    assert chunks[0].text == block
    assert chunks[0].metadata["oversized_atomic_block"]
    assert chunks[0].metadata["warnings"] == ["unclosed_code_fence"]
