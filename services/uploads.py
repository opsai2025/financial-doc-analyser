"""Turn uploaded PDFs / a ZIP into validated in-memory files (D-10.4)."""

from __future__ import annotations

import hashlib
import io
import posixpath
import zipfile
from dataclasses import dataclass, field

from core.fa import fa_digits
from services.app_settings import UploadLimits, upload_limits

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


def _check_pdf(name: str, data: bytes, intake: Intake, limits: UploadLimits) -> None:
    if len(data) > limits.max_pdf_mb * MB:
        intake.rejected.append(f"«{name}»: حجم بیش از {fa_digits(limits.max_pdf_mb)} مگابایت است.")
    elif not data.startswith(b"%PDF-"):
        intake.rejected.append(f"«{name}»: فایل PDF معتبر نیست.")
    elif len(intake.files) >= limits.max_files:
        intake.rejected.append(f"«{name}»: بیش از {fa_digits(limits.max_files)} فایل در یک درخواست مجاز نیست.")
    else:
        intake.files.append(IncomingFile(name, data))


def from_pdfs(uploads: list[tuple[str, bytes]], limits: UploadLimits | None = None) -> Intake:
    """`limits` = the admin's current upload limits unless given."""
    limits = limits or upload_limits()
    intake = Intake()
    for name, data in uploads:
        _check_pdf(name, data, intake, limits)
    return intake


def _zip_name(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if not info.flag_bits & 0x800:  # not flagged UTF-8: Windows tools often store UTF-8 bytes as cp437
        try:
            name = name.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return posixpath.basename(name.replace("\\", "/"))


def from_zip(data: bytes, limits: UploadLimits | None = None) -> Intake:
    limits = limits or upload_limits()
    intake = Intake()
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        intake.rejected.append("فایل ZIP معتبر نیست.")
        return intake
    with archive:
        entries = [i for i in archive.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX/")]
        if sum(i.file_size for i in entries) > limits.max_zip_mb * MB:
            intake.rejected.append(f"حجم محتوای ZIP بیش از {fa_digits(limits.max_zip_mb)} مگابایت است.")
            return intake
        for info in entries:
            name = _zip_name(info)
            if not name.lower().endswith(".pdf"):
                intake.rejected.append(f"«{name}»: فایل PDF نیست و نادیده گرفته شد.")
                continue
            if info.file_size > limits.max_pdf_mb * MB:
                intake.rejected.append(f"«{name}»: حجم بیش از {fa_digits(limits.max_pdf_mb)} مگابایت است.")
                continue
            with archive.open(info) as handle:
                content = handle.read(limits.max_pdf_mb * MB + 1)  # never trust the declared size
            _check_pdf(name, content, intake, limits)
    return intake
