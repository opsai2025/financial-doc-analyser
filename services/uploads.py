"""Turn uploaded PDFs / a ZIP into validated in-memory files (D-10.4)."""

from __future__ import annotations

import hashlib
import io
import posixpath
import zipfile
from dataclasses import dataclass, field

from core.config import MAX_FILES_PER_BATCH, MAX_PDF_MB, MAX_ZIP_UNCOMPRESSED_MB
from core.fa import fa_digits

MB = 1024 * 1024


@dataclass(frozen=True)
class IncomingFile:
    name: str
    data: bytes = field(repr=False)

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass
class Intake:
    files: list[IncomingFile] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)  # Persian messages, one per skipped entry

    @property
    def ok(self) -> bool:
        return bool(self.files)


def _check_pdf(name: str, data: bytes, intake: Intake) -> None:
    if len(data) > MAX_PDF_MB * MB:
        intake.rejected.append(f"«{name}»: حجم بیش از {fa_digits(MAX_PDF_MB)} مگابایت است.")
    elif not data.startswith(b"%PDF-"):
        intake.rejected.append(f"«{name}»: فایل PDF معتبر نیست.")
    elif len(intake.files) >= MAX_FILES_PER_BATCH:
        intake.rejected.append(f"«{name}»: بیش از {fa_digits(MAX_FILES_PER_BATCH)} فایل در یک درخواست مجاز نیست.")
    else:
        intake.files.append(IncomingFile(name, data))


def from_pdfs(uploads: list[tuple[str, bytes]]) -> Intake:
    intake = Intake()
    for name, data in uploads:
        _check_pdf(name, data, intake)
    return intake


def _zip_name(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if not info.flag_bits & 0x800:  # not flagged UTF-8: Windows tools often store UTF-8 bytes as cp437
        try:
            name = name.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return posixpath.basename(name.replace("\\", "/"))


def from_zip(data: bytes) -> Intake:
    intake = Intake()
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        intake.rejected.append("فایل ZIP معتبر نیست.")
        return intake
    with archive:
        entries = [i for i in archive.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX/")]
        if sum(i.file_size for i in entries) > MAX_ZIP_UNCOMPRESSED_MB * MB:
            intake.rejected.append(f"حجم محتوای ZIP بیش از {fa_digits(MAX_ZIP_UNCOMPRESSED_MB)} مگابایت است.")
            return intake
        for info in entries:
            name = _zip_name(info)
            if not name.lower().endswith(".pdf"):
                intake.rejected.append(f"«{name}»: فایل PDF نیست و نادیده گرفته شد.")
                continue
            if info.file_size > MAX_PDF_MB * MB:
                intake.rejected.append(f"«{name}»: حجم بیش از {fa_digits(MAX_PDF_MB)} مگابایت است.")
                continue
            with archive.open(info) as handle:
                content = handle.read(MAX_PDF_MB * MB + 1)  # never trust the declared size
            _check_pdf(name, content, intake)
    return intake
