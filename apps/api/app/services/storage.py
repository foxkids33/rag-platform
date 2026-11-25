from __future__ import annotations

from typing import BinaryIO

from minio import Minio
from minio.error import S3Error

from app.core.config import settings


class StorageError(RuntimeError):
    """Raised when object storage cannot complete an operation."""


class ObjectStorage:
    def __init__(self) -> None:
        self._client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        self._bucket = settings.minio_bucket

    def ensure_bucket(self) -> None:
        try:
            if self._client.bucket_exists(self._bucket):
                return
            self._client.make_bucket(self._bucket)
        except S3Error as exc:
            # Another API process may create the bucket between exists() and make_bucket().
            if exc.code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise StorageError("Failed to prepare object storage bucket") from exc
        except Exception as exc:
            raise StorageError("Failed to prepare object storage bucket") from exc

    def upload(
        self,
        *,
        object_key: str,
        stream: BinaryIO,
        size: int,
        content_type: str,
    ) -> None:
        self.ensure_bucket()
        try:
            self._client.put_object(
                bucket_name=self._bucket,
                object_name=object_key,
                data=stream,
                length=size,
                content_type=content_type,
            )
        except Exception as exc:
            raise StorageError("Failed to upload object") from exc

    def delete(self, object_key: str) -> None:
        try:
            self._client.remove_object(self._bucket, object_key)
        except Exception as exc:
            raise StorageError("Failed to delete object") from exc


storage = ObjectStorage()
