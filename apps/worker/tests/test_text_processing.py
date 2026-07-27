from app.text_processing import extract_text, split_into_chunks


def test_extract_html_text() -> None:
    text = extract_text("page.html", b"<h1>Title</h1><p>Hello world</p>")
    assert "Title" in text
    assert "Hello world" in text
    assert "<h1>" not in text


def test_split_into_chunks_keeps_content() -> None:
    source = "Paragraph one. " * 300
    chunks = split_into_chunks(source)
    assert len(chunks) > 1
    assert all(chunk.strip() for chunk in chunks)


def test_plain_document_builds_parent_child_chunks(monkeypatch):
    from app import document_processing

    monkeypatch.setattr(document_processing.settings, "chunk_max_chars", 80)
    monkeypatch.setattr(document_processing.settings, "chunk_overlap_chars", 10)
    monkeypatch.setattr(document_processing.settings, "chunk_min_chars", 1)
    monkeypatch.setattr(document_processing.settings, "parent_max_chars", 180)

    data = (
        "Первый абзац содержит описание тестовой системы хранения данных.\n\n"
        "Второй абзац описывает резервное копирование и восстановление.\n\n"
        "Третий абзац содержит сведения о контроллерах и дисковых модулях."
    ).encode("utf-8")

    chunks = document_processing.parse_document("sample.txt", data)

    assert len(chunks) >= 2
    assert all(chunk.parent_chunk_id for chunk in chunks)
    assert all(chunk.parent_text for chunk in chunks)
    assert all(chunk.metadata["chunking"] == "parent_child_v1" for chunk in chunks)
    assert all(chunk.metadata["parser"] == "plain_text_v2" for chunk in chunks)
