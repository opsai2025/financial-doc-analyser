"""PDF storage: Supabase Storage (private bucket) or a local folder (D-3.5, D-10.13).

Files are stored once per SHA-256 (path ``ab/cd/<sha256>.pdf``); per-user accounting lives in
`user_documents`.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import httpx

from core.config import get_settings

log = logging.getLogger(__name__)


def object_path(document_id: str) -> str:
    return f"{document_id[:2]}/{document_id[2:4]}/{document_id}.pdf"


class Storage(Protocol):
    def put(self, path: str, data: bytes) -> None: ...
    def get(self, path: str) -> bytes | None: ...
    def delete(self, path: str) -> None: ...


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root

    def _file(self, path: str) -> Path:
        target = (self.root / path).resolve()
        if self.root.resolve() not in target.parents:
            raise ValueError("invalid storage path")
        return target

    def put(self, path: str, data: bytes) -> None:
        target = self._file(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(target)

    def get(self, path: str) -> bytes | None:
        target = self._file(path)
        return target.read_bytes() if target.exists() else None

    def delete(self, path: str) -> None:
        self._file(path).unlink(missing_ok=True)


class SupabaseStorage:
    """Supabase Storage REST API with the service key (server-side only)."""

    def __init__(self, url: str, service_key: str, bucket: str, transport: httpx.BaseTransport | None = None):
        self.bucket = bucket
        self.client = httpx.Client(
            base_url=url.rstrip("/") + "/storage/v1",
            headers={"Authorization": f"Bearer {service_key}", "apikey": service_key},
            timeout=120,
            transport=transport,
        )
        self._bucket_ready = False

    def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        response = self.client.get(f"/bucket/{self.bucket}")
        if response.status_code in (400, 404):
            created = self.client.post("/bucket", json={"id": self.bucket, "name": self.bucket, "public": False})
            if created.status_code not in (200, 201, 409):
                created.raise_for_status()
        else:
            response.raise_for_status()
        self._bucket_ready = True

    def put(self, path: str, data: bytes) -> None:
        self._ensure_bucket()
        response = self.client.post(
            f"/object/{self.bucket}/{path}", content=data,
            headers={"Content-Type": "application/pdf", "x-upsert": "true"},
        )
        response.raise_for_status()

    def get(self, path: str) -> bytes | None:
        response = self.client.get(f"/object/{self.bucket}/{path}")
        if response.status_code in (400, 404):
            return None
        response.raise_for_status()
        return response.content

    def delete(self, path: str) -> None:
        response = self.client.request("DELETE", f"/object/{self.bucket}", json={"prefixes": [path]})
        if response.status_code not in (200, 404):
            response.raise_for_status()


@lru_cache(maxsize=1)
def get_storage() -> Storage:
    settings = get_settings()
    if settings.supabase_url and settings.supabase_service_key:
        return SupabaseStorage(settings.supabase_url, settings.supabase_service_key, settings.supabase_storage_bucket)
    return LocalStorage(settings.local_storage_dir)
