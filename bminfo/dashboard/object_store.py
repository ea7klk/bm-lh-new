"""Object-store access for archived raw events.

The archive lives in a Silo bucket (S3-compatible). This module is the only
place that talks to it, and it exposes a small interface so the archiver can be
tested against an in-memory double rather than the network.

Silo is MinIO-compatible: path-style addressing, a region that is accepted but
unused, and `Content-MD5`-free simple uploads. Every object carries the row
count and SHA-256 of its contents as user metadata, which is what the archiver
verifies before deleting anything from PostgreSQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Protocol

from django.conf import settings


class ObjectStoreNotConfigured(RuntimeError):
    """Raised when archiving is attempted without complete credentials."""


class ObjectStoreError(RuntimeError):
    """Raised when an upload or download does not behave as expected."""


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    rows: int | None
    sha256: str | None


class ObjectStore(Protocol):
    def put_file(self, key: str, path: str, *, rows: int, sha256: str) -> None: ...
    def download_to(self, key: str, path: str) -> None: ...
    def head(self, key: str) -> StoredObject | None: ...
    def list_prefix(self, prefix: str) -> Iterator[StoredObject]: ...
    def delete(self, key: str) -> None: ...
    def ping(self) -> None: ...


def storage_configured() -> bool:
    return all(
        [
            settings.S3_ENDPOINT_URL,
            settings.S3_BUCKET,
            settings.S3_ACCESS_KEY_ID,
            settings.S3_SECRET_ACCESS_KEY,
        ]
    )


def configuration_problem() -> str | None:
    """A human-readable reason archiving is unavailable, or None."""
    missing = []
    if not settings.S3_ENDPOINT_URL:
        missing.append("S3_ENDPOINT_URL")
    if not settings.S3_BUCKET:
        missing.append("S3_BUCKET")
    if not settings.S3_ACCESS_KEY_ID:
        missing.append("S3_ACCESS_KEY_ID")
    if not settings.S3_SECRET_ACCESS_KEY:
        missing.append("S3_SECRET_ACCESS_KEY")
    if missing:
        return "object storage is not configured; missing " + ", ".join(missing)
    return None


class S3ObjectStore:
    """Silo/MinIO client. The boto3 client is created lazily and reused."""

    def __init__(self):
        problem = configuration_problem()
        if problem:
            raise ObjectStoreNotConfigured(problem)
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "s3",
                endpoint_url=settings.S3_ENDPOINT_URL,
                aws_access_key_id=settings.S3_ACCESS_KEY_ID,
                aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
                region_name=settings.S3_REGION,
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": settings.S3_ADDRESSING_STYLE},
                    retries={"max_attempts": 5, "mode": "standard"},
                    connect_timeout=10,
                    read_timeout=120,
                ),
            )
        return self._client

    def ping(self) -> None:
        """Raise if the bucket is missing or the credentials are wrong."""
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self.client.head_bucket(Bucket=settings.S3_BUCKET)
        except ClientError as exc:
            raise ObjectStoreError(
                f"cannot reach bucket {settings.S3_BUCKET!r}: {exc.response.get('Error', {}).get('Code', exc)}"
            ) from exc
        except BotoCoreError as exc:
            raise ObjectStoreError(f"cannot reach {settings.S3_ENDPOINT_URL}: {exc}") from exc

    def put_file(self, key: str, path: str, *, rows: int, sha256: str) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            with open(path, "rb") as handle:
                # boto3's transfer manager does not accept ContentMD5 here, and
                # it is not needed: the archiver re-downloads the object and
                # compares its SHA-256 before deleting any row.
                self.client.upload_fileobj(
                    handle,
                    settings.S3_BUCKET,
                    key,
                    ExtraArgs={
                        "ContentType": "application/gzip",
                        "Metadata": {"rows": str(rows), "sha256": sha256},
                    },
                )
        except (ClientError, BotoCoreError) as exc:
            raise ObjectStoreError(f"upload of {key} failed: {exc}") from exc

    def download_to(self, key: str, path: str) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            with open(path, "wb") as handle:
                self.client.download_fileobj(settings.S3_BUCKET, key, handle)
        except (ClientError, BotoCoreError) as exc:
            raise ObjectStoreError(f"download of {key} failed: {exc}") from exc

    def head(self, key: str) -> StoredObject | None:
        from botocore.exceptions import ClientError

        try:
            head = self.client.head_object(Bucket=settings.S3_BUCKET, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise ObjectStoreError(f"head of {key} failed: {exc}") from exc
        metadata = head.get("Metadata", {}) or {}
        rows = metadata.get("rows")
        return StoredObject(
            key=key,
            size=head.get("ContentLength", 0),
            rows=int(rows) if rows and str(rows).isdigit() else None,
            sha256=metadata.get("sha256"),
        )

    def list_prefix(self, prefix: str) -> Iterator[StoredObject]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=settings.S3_BUCKET, Prefix=prefix):
            for item in page.get("Contents", []) or []:
                yield StoredObject(key=item["Key"], size=item.get("Size", 0), rows=None, sha256=None)

    def delete(self, key: str) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self.client.delete_object(Bucket=settings.S3_BUCKET, Key=key)
        except (ClientError, BotoCoreError) as exc:
            raise ObjectStoreError(f"delete of {key} failed: {exc}") from exc


_store: S3ObjectStore | None = None


def get_store() -> S3ObjectStore:
    """Process-wide store, created once so the connection pool is reused."""
    global _store
    if _store is None:
        _store = S3ObjectStore()
    return _store


def reset_store() -> None:
    """Drop the cached client (tests, and after a credential change)."""
    global _store
    _store = None
