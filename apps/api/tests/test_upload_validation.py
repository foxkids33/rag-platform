import pytest
from fastapi import HTTPException

from app.services.upload_validation import ALLOWED_EXTENSIONS, validated_content_type


@pytest.mark.parametrize("filename,header,mime", [
    ("scan.PNG", b"\x89PNG\r\n\x1a\n", "image/png"),
    ("photo.jpg", b"\xff\xd8\xff\xe0", "image/jpeg"),
    ("photo.jpeg", b"\xff\xd8\xff\xe1", "image/jpeg"),
    ("diagram.webp", b"RIFF1234WEBP", "image/webp"),
    ("scan.tiff", b"II*\x00", "image/tiff"),
    ("scan.tif", b"MM\x00*", "image/tiff"),
    ("manual.pdf", b"%PDF-1.7\n", "application/pdf"),
    ("manual.docx", b"PK\x03\x04", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
])
def test_binary_upload_checks_signature_and_sets_canonical_mime(filename, header, mime):
    assert validated_content_type(filename, header, "text/html") == mime


@pytest.mark.parametrize("filename", ["photo.jpg", "scan.png", "scan.tiff", "diagram.webp", "manual.pdf", "manual.docx"])
def test_extension_alone_is_not_trusted(filename):
    with pytest.raises(HTTPException) as error:
        validated_content_type(filename, b"<html>not an image</html>", "image/png")
    assert error.value.status_code == 415


def test_all_advertised_formats_are_accepted():
    assert {".pdf", ".docx", ".txt", ".md", ".csv", ".json", ".html", ".xml", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"} <= ALLOWED_EXTENSIONS
    assert ".svg" not in ALLOWED_EXTENSIONS  # Active content is deliberately excluded.
