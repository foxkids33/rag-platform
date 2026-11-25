from io import BytesIO
from zipfile import ZipFile

from app.document_processing import parse_document
from app.text_processing import extract_text, normalize_exact_text, split_into_chunks


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


def test_normalize_exact_text_removes_identifier_punctuation() -> None:
    assert normalize_exact_text("BLUE-KB_942 / МБД.Х") == "bluekb942мбдх"


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


def test_docx_uses_lightweight_openxml_parser_and_preserves_headings_and_tables():
    document_xml = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body>
        <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Runbook</w:t></w:r></w:p>
        <w:p><w:r><w:t>Recovery code BLUE-KB-942</w:t></w:r></w:p>
        <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Host</w:t></w:r></w:p></w:tc>
          <w:tc><w:p><w:r><w:t>Status</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
      </w:body>
    </w:document>'''
    archive = BytesIO()
    with ZipFile(archive, "w") as docx:
        docx.writestr("word/document.xml", document_xml)

    chunks = parse_document("runbook.docx", archive.getvalue())

    assert chunks
    assert all(chunk.metadata["parser"] == "docx_xml_v1" for chunk in chunks)
    assert all(chunk.heading_breadcrumb == "Runbook" for chunk in chunks)
    combined = "\n".join(chunk.text for chunk in chunks)
    assert "Recovery code BLUE-KB-942" in combined
    assert "Host | Status" in combined
