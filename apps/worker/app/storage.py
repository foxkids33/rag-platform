from __future__ import annotations

from minio import Minio

from app.config import settings


class ObjectStorage:
    def __init__(self) -> None:
        self._client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )

    def download(self, object_key: str) -> bytes:
        response = self._client.get_object(settings.minio_bucket, object_key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()


storage = ObjectStorage()
