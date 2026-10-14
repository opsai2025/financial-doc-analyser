import io
import zipfile

import httpx
import pytest

from services.storage import LocalStorage, SupabaseStorage, object_path
from services.app_settings import UploadLimits
from services.uploads import from_pdfs, from_zip


def test_local_storage(tmp_path):
    storage = LocalStorage(tmp_path)
    path = object_path("ab" * 32)
    storage.put(path, b"%PDF-data")
    assert storage.get(path) == b"%PDF-data"
    storage.delete(path)
    assert storage.get(path) is None
    with pytest.raises(ValueError):
        storage.put("../escape.pdf", b"x")


def test_supabase_storage_requests():
    calls = []
    store = {}

    def handler(request: httpx.Request):
        calls.append((request.method, request.url.path))
        assert request.headers["apikey"] == "svc"
        if request.url.path.endswith("/bucket/pdfs"):
            return httpx.Response(404)
        if request.url.path.endswith("/bucket"):
            return httpx.Response(200, json={"name": "pdfs"})
        if request.method == "POST":
            store[request.url.path] = request.content
            return httpx.Response(200, json={})
        if request.method == "GET":
            return httpx.Response(200, content=store[request.url.path]) if request.url.path in store else httpx.Response(400)
        return httpx.Response(200, json=[])

    s = SupabaseStorage("https://x.supabase.co", "svc", "pdfs", transport=httpx.MockTransport(handler))
    s.put("ab/cd/f.pdf", b"%PDF-1")
    assert s.get("ab/cd/f.pdf") == b"%PDF-1"
    assert s.get("missing.pdf") is None
    s.delete("ab/cd/f.pdf")
    assert ("POST", "/storage/v1/bucket") in calls and ("DELETE", "/storage/v1/object/pdfs") in calls


def test_uploads_validation():
    intake = from_pdfs([("a.pdf", b"%PDF-1 a"), ("b.pdf", b"not a pdf")], UploadLimits())
    assert [f.name for f in intake.files] == ["a.pdf"] and len(intake.rejected) == 1

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("folder/۱۳۹۸.pdf", b"%PDF-1 x")
        zf.writestr("notes.txt", b"hi")
        zf.writestr("__MACOSX/._x.pdf", b"junk")
    intake = from_zip(buffer.getvalue(), UploadLimits())
    assert [f.name for f in intake.files] == ["۱۳۹۸.pdf"]
    assert len(intake.rejected) == 1
    assert from_zip(b"garbage", UploadLimits()).rejected
