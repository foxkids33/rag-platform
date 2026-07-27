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
