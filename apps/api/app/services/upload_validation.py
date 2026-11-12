"""Cheap binary signature checks before storing an upload.

The worker performs full bounded parsing. Never treat a browser-supplied MIME
type or a filename extension as proof of the actual file format.
"""

from pathlib import Path

from fastapi import HTTPException

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}
ALLOWED_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".json", ".html", ".htm", ".xml", ".pdf", ".docx",
    *IMAGE_EXTENSIONS,
}


def validated_content_type(filename: str, header: bytes, supplied: str | None) -> str:
    extension = Path(filename).suffix.lower()
    signatures = {
        ".pdf": (header.lstrip().startswith(b"%PDF-"), "application/pdf"),
        ".docx": (header.startswith(b"PK\x03\x04"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ".png": (header.startswith(b"\x89PNG\r\n\x1a\n"), "image/png"),
        ".jpg": (header.startswith(b"\xff\xd8\xff"), "image/jpeg"),
        ".jpeg": (header.startswith(b"\xff\xd8\xff"), "image/jpeg"),
        ".webp": (header.startswith(b"RIFF") and header[8:12] == b"WEBP", "image/webp"),
        ".tif": (header.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")), "image/tiff"),
        ".tiff": (header.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")), "image/tiff"),
    }
    if extension in signatures:
        valid, content_type = signatures[extension]
        if not valid:
            raise HTTPException(status_code=415, detail="File content does not match its extension")
        return content_type
    # Originals stay in the private object store; no uploaded HTML is served
    # inline under the application's origin.
    return supplied or "application/octet-stream"
