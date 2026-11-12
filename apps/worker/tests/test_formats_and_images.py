from __future__ import annotations

import base64
import json
import shutil
import subprocess
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import httpx
import pytest
from app import media_processing
from app.config import Settings, settings
from app.document_processing import parse_document
from PIL import Image, ImageDraw, ImageFont


def image_bytes(format="PNG", size=(900, 200), text="BLUE-KB-942 SERVICE READY"):
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", size=36)
    except OSError:
        font = ImageFont.load_default(size=32)
    draw.text((20, 65), text, fill="black", font=font)
    output = BytesIO()
    image.save(output, format=format)
    image.close()
    return output.getvalue()


def text_pdf(text="BLUE-KB-942 native PDF text"):
    """Generate a valid small text PDF without pulling in a document SDK."""
    content = f"BT /F1 16 Tf 40 120 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(content)).encode()
        + b" >>\nstream\n"
        + content
        + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    )
    return bytes(output)


@pytest.fixture(autouse=True)
def no_external_vision(monkeypatch):
    monkeypatch.setattr(settings, "vision_enabled", False)


@pytest.mark.parametrize(
    "extension,data,expected",
    [
        ("txt", "Русский текст BLUE-KB-942".encode(), "Русский текст"),
        ("txt", "Русский текст BLUE-KB-942".encode("cp1251"), "Русский текст"),
        ("txt", "Русский текст BLUE-KB-942".encode("utf-16"), "Русский текст"),
        ("md", b"# Runbook\n\nBLUE-KB-942 is ready", "BLUE-KB-942"),
        ("csv", b'host;status\n"node-1";"ready, yes"', "node-1 | ready, yes"),
        (
            "json",
            json.dumps({"сервис": "BLUE-KB-942"}, ensure_ascii=False).encode(),
            '"сервис"',
        ),
        (
            "html",
            b"<h1>BLUE-KB-942</h1><script>SECRET_SCRIPT</script><p>Ready</p>",
            "Ready",
        ),
        ("htm", b"<h1>BLUE-KB-942</h1>", "BLUE-KB-942"),
        (
            "xml",
            "<root><service>BLUE-KB-942</service><status>Готов</status></root>".encode(),
            "Готов",
        ),
    ],
)
def test_all_lightweight_formats(extension, data, expected):
    chunks = parse_document("test." + extension, data)
    assert chunks
    assert all(chunk.text and chunk.parent_text for chunk in chunks)
    combined = "\n".join(chunk.text for chunk in chunks)
    assert expected in combined
    assert "SECRET_SCRIPT" not in combined


def test_native_pdf_works_without_docling_or_ocr(monkeypatch):
    monkeypatch.setattr(
        media_processing, "_ocr", lambda *_: pytest.fail("Native PDF must not need OCR")
    )
    chunks = parse_document("runbook.pdf", text_pdf())
    assert "BLUE-KB-942 native PDF text" in chunks[0].text
    assert chunks[0].metadata["parser"] == "pdf_cpu_v1"
    assert chunks[0].metadata["extraction_methods"] == ["pdf_text"]
    assert chunks[0].page_start == 1


def test_scanned_pdf_renders_then_ocr(monkeypatch):
    monkeypatch.setattr(
        media_processing, "_ocr", lambda *_: ("SCANNED BLUE-KB-942", [])
    )
    chunks = parse_document("scan.pdf", image_bytes("PDF"))
    assert "SCANNED BLUE-KB-942" in chunks[0].text
    assert chunks[0].metadata["extraction_methods"] == ["ocr"]
    assert chunks[0].metadata["derived_content"] is True


@pytest.mark.parametrize(
    "extension,format",
    [
        ("png", "PNG"),
        ("jpg", "JPEG"),
        ("jpeg", "JPEG"),
        ("webp", "WEBP"),
        ("tif", "TIFF"),
        ("tiff", "TIFF"),
    ],
)
def test_all_image_formats(extension, format, monkeypatch):
    monkeypatch.setattr(
        media_processing, "_ocr", lambda *_: ("Изображение BLUE-KB-942", [])
    )
    chunks = parse_document("attached." + extension, image_bytes(format))
    assert "Изображение BLUE-KB-942" in chunks[0].text
    assert chunks[0].metadata["extraction_methods"] == ["ocr"]
    assert chunks[0].metadata["image_width"] == 900
    assert "возможны ошибки" in chunks[0].text


def test_vision_description_separate_from_ocr_and_only_data_url(monkeypatch):
    monkeypatch.setattr(settings, "vision_enabled", True)
    monkeypatch.setattr(settings, "vision_base_url", "http://corporate-model:8000/v1")
    monkeypatch.setattr(settings, "vision_model", "vision-test")
    monkeypatch.setattr(settings, "vision_enable_thinking", False)
    monkeypatch.setattr(media_processing, "_ocr", lambda *_: ("NODE-A", []))
    captured = []
    client_class = httpx.Client

    def respond(request):
        captured.append(request)
        payload = json.loads(request.content)
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}
        assert payload["messages"][1]["content"][1]["image_url"]["url"].startswith(
            "data:image/jpeg;base64,"
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "Два узла соединены стрелкой."},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    monkeypatch.setattr(
        media_processing.httpx,
        "Client",
        lambda **kwargs: client_class(
            transport=httpx.MockTransport(respond),
            **kwargs,
        ),
    )
    chunks = parse_document("diagram.png", image_bytes())
    text = "\n".join(chunk.text for chunk in chunks)
    assert "NODE-A" in text and "Два узла" in text
    assert "интерпретация, требует проверки" in text
    assert chunks[0].metadata["extraction_methods"] == ["ocr", "vision"]
    assert len(captured) == 1
    assert str(captured[0].url) == "http://corporate-model:8000/v1/chat/completions"


def test_vision_failure_keeps_available_ocr(monkeypatch):
    monkeypatch.setattr(media_processing, "_ocr", lambda *_: ("OCR DATA", []))
    monkeypatch.setattr(
        media_processing,
        "_vision",
        lambda *_: ("", ["vision_endpoint_rejected_request"]),
    )
    chunks = parse_document("image.png", image_bytes())
    assert "OCR DATA" in chunks[0].text
    assert "vision_endpoint_rejected_request" in chunks[0].metadata["warnings"]


def test_vision_preview_size_budget_and_default_payload(monkeypatch):
    monkeypatch.setattr(settings, "vision_enabled", True)
    monkeypatch.setattr(settings, "vision_base_url", "https://corporate-model.test")
    monkeypatch.setattr(settings, "vision_model", "vision-test")
    monkeypatch.setattr(settings, "vision_enable_thinking", None)
    monkeypatch.setattr(settings, "vision_max_image_edge", 640)
    monkeypatch.setattr(settings, "vision_max_images_per_document", 1)
    captured = []
    client_class = httpx.Client

    def respond(request):
        captured.append(request)
        payload = json.loads(request.content)
        assert "chat_template_kwargs" not in payload
        uri = payload["messages"][1]["content"][1]["image_url"]["url"]
        with Image.open(BytesIO(base64.b64decode(uri.split(",", 1)[1]))) as preview:
            assert max(preview.size) <= 640
            assert preview.format == "JPEG"
            assert not preview.getexif()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "Видимая схема."},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return client_class(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(media_processing.httpx, "Client", client)
    image = Image.new("RGB", (3000, 2000), "white")
    try:
        budget = media_processing.MediaBudget()
        assert media_processing._vision(image, budget) == ("Видимая схема.", [])
        assert media_processing._vision(image, budget) == (
            "",
            ["vision_image_budget_exceeded"],
        )
    finally:
        image.close()
    assert len(captured) == 1


def test_vision_truncated_description_is_not_indexed(monkeypatch):
    monkeypatch.setattr(settings, "vision_enabled", True)
    monkeypatch.setattr(settings, "vision_base_url", "https://corporate-model.test")
    monkeypatch.setattr(settings, "vision_model", "vision-test")
    monkeypatch.setattr(media_processing, "_ocr", lambda *_: ("OCR retained", []))
    client_class = httpx.Client
    monkeypatch.setattr(
        media_processing.httpx,
        "Client",
        lambda **kwargs: client_class(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "content": "An incomplete potentially misleading description"
                                },
                                "finish_reason": "length",
                            }
                        ]
                    },
                )
            ),
            **kwargs,
        ),
    )
    chunks = parse_document("diagram.png", image_bytes())
    assert "OCR retained" in chunks[0].text
    assert "incomplete" not in chunks[0].text
    assert "vision_response_truncated" in chunks[0].metadata["warnings"]


def test_empty_image_does_not_become_ready_metadata_only(monkeypatch):
    monkeypatch.setattr(
        media_processing, "_ocr", lambda *_: ("", ["ocr_no_readable_text"])
    )
    with pytest.raises(ValueError, match="no readable text or visual description"):
        parse_document("empty.png", image_bytes())


def test_ocr_process_has_timeout_no_shell_and_limited_threads(monkeypatch):
    monkeypatch.setattr(
        media_processing.shutil, "which", lambda _: "/usr/bin/tesseract"
    )
    captured = []

    def run(arguments, **kwargs):
        captured.append((arguments, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"OCR BLUE-KB-942", stderr=b"")

    monkeypatch.setattr(media_processing.subprocess, "run", run)
    chunks = parse_document("image.png", image_bytes())
    arguments, kwargs = captured[0]
    assert arguments[:3] == ["/usr/bin/tesseract", "stdin", "stdout"]
    assert kwargs["timeout"] <= settings.ocr_timeout_seconds
    assert not kwargs.get("shell")
    assert kwargs["env"]["OMP_THREAD_LIMIT"] == "2"
    assert chunks[0].metadata["extraction_methods"] == ["ocr"]


def test_ocr_timeout_fails_without_invented_text(monkeypatch):
    monkeypatch.setattr(
        media_processing.shutil, "which", lambda _: "/usr/bin/tesseract"
    )

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("tesseract", 1)

    monkeypatch.setattr(media_processing.subprocess, "run", timeout)
    with pytest.raises(ValueError, match="ocr_timeout"):
        parse_document("image.png", image_bytes())


@pytest.mark.parametrize(
    "filename,data",
    [
        ("bad.pdf", b"%PDF-1.7\nnot a PDF"),
        ("bad.docx", b"not an OpenXML archive"),
        ("bad.png", b"not a picture"),
        ("bad.json", b'{"not_valid":'),
        ("bad.xml", b"<root>unclosed"),
        ("bad.txt", b"binary\x00content"),
        ("empty.txt", b""),
        ("empty.txt", b"    \n\n"),
        ("bad.exe", b"something"),
    ],
)
def test_invalid_or_empty_documents_fail(filename, data):
    with pytest.raises(ValueError):
        parse_document(filename, data)


def test_xml_external_entity_is_rejected():
    data = b'<!DOCTYPE root [<!ENTITY secret SYSTEM "file:///etc/passwd">]><root>&secret;</root>'
    with pytest.raises(ValueError, match="forbidden"):
        parse_document("bad.xml", data)


def test_docx_utf16_entity_is_rejected():
    source = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE root [<!ENTITY a "secret">]><root>&a;</root>'
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("word/document.xml", source.encode("utf-16"))
    with pytest.raises(ValueError, match="forbidden"):
        parse_document("bad.docx", output.getvalue())


def test_image_extension_must_match_content():
    with pytest.raises(ValueError, match="does not match"):
        parse_document("wrong.jpg", image_bytes("PNG"))


def test_pixel_limit_applied_before_decode(monkeypatch):
    monkeypatch.setattr(settings, "image_max_pixels", 10_000)
    with pytest.raises(ValueError, match="pixel safety limit"):
        parse_document("large.png", image_bytes())


def test_pdf_page_limit(monkeypatch):
    monkeypatch.setattr(settings, "pdf_max_pages", 0)
    with pytest.raises(ValueError, match="page limit"):
        parse_document("native.pdf", text_pdf())


def test_ocr_page_budget_is_not_silently_truncated(monkeypatch):
    monkeypatch.setattr(settings, "ocr_max_pages", 0)
    with pytest.raises(ValueError, match="OCR limit"):
        parse_document("image.png", image_bytes())


def test_multiframe_tiff_keeps_all_pages(monkeypatch):
    monkeypatch.setattr(media_processing, "_ocr", lambda *_: ("PAGE OCR", []))
    first = Image.new("RGB", (200, 120), "white")
    second = Image.new("RGB", (200, 120), "white")
    output = BytesIO()
    first.save(output, format="TIFF", save_all=True, append_images=[second])
    chunks = parse_document("scan.tiff", output.getvalue())
    assert {chunk.page_start for chunk in chunks} == {1, 2}
    monkeypatch.setattr(settings, "image_max_frames", 1)
    with pytest.raises(ValueError, match="frame limit"):
        parse_document("scan.tiff", output.getvalue())


def test_docx_archive_expansion_limit(monkeypatch):
    from app import document_processing

    real_zip = document_processing.ZipFile

    class OversizedArchive:
        def __init__(self, *_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def infolist(self):
            return [SimpleNamespace(file_size=101 * 1024 * 1024)]

    monkeypatch.setattr(document_processing, "ZipFile", OversizedArchive)
    with pytest.raises(ValueError, match="expansion limit"):
        parse_document("bomb.docx", b"fake archive header")
    monkeypatch.setattr(document_processing, "ZipFile", real_zip)


def test_vision_requires_explicit_http_opt_in_and_model():
    with pytest.raises(ValueError, match="HTTP vision"):
        Settings(
            vision_enabled=True,
            vision_base_url="http://corporate-model:8000",
            vision_model="vision",
        )


@pytest.mark.skipif(
    not shutil.which("tesseract"),
    reason="Local Tesseract is not installed; Docker image includes it",
)
def test_real_tesseract_image_ocr(monkeypatch):
    monkeypatch.setattr(settings, "ocr_languages", "eng")
    chunks = parse_document("actual.png", image_bytes())
    text = "\n".join(chunk.text for chunk in chunks)
    assert "BLUE" in text and "942" in text


@pytest.mark.skipif(
    not shutil.which("tesseract"),
    reason="Local Tesseract is not installed; Docker image includes it",
)
def test_real_tesseract_scanned_pdf(monkeypatch):
    monkeypatch.setattr(settings, "ocr_languages", "eng")
    chunks = parse_document("actual-scan.pdf", image_bytes("PDF"))
    text = "\n".join(chunk.text for chunk in chunks)
    assert "BLUE" in text and "942" in text
    assert "ocr" in chunks[0].metadata["extraction_methods"]


@pytest.mark.skipif(
    not shutil.which("tesseract"),
    reason="Local Tesseract is not installed; Docker image includes it",
)
def test_real_tesseract_russian_ocr(monkeypatch):
    monkeypatch.setattr(settings, "ocr_languages", "rus+eng")
    chunks = parse_document(
        "russian.png", image_bytes(text="Проверка подключения к серверу")
    )
    text = "\n".join(chunk.text for chunk in chunks)
    assert "проверка" in text.lower() and "подключения" in text.lower()
