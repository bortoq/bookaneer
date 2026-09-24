"""Local book inventory and file downloads."""

from collections import Counter
import os
from pathlib import Path
import re
import tempfile
import unicodedata
import zipfile

from .catalog import FORMATS
from .transport import DownloadCancelled

BOOK_LIMIT = 512 * 1024 * 1024
EXTRACT_LIMIT = 1024 * 1024 * 1024

def safe_name(title):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")
    return name[:100] or "Книга"


def normalized_title(title):
    text = unicodedata.normalize("NFKC", title).casefold().replace("ё", "е")
    return " ".join(re.findall(r"\w+", text.replace("_", " ")))


def file_inventory(root):
    """Index book files in root and all its subdirectories by ID or title."""
    by_id = set()
    by_title = set()
    for directory, subdirs, filenames in os.walk(root):
        subdirs[:] = [name for name in subdirs if name != ".git"]
        for filename in filenames:
            path = Path(directory) / filename
            if not path.is_file() or path.stat().st_size == 0:
                continue
            name = filename
            suffix = Path(name).suffix.lower()
            if suffix in (".zip", ".rar"):
                name = name[:-len(suffix)]
                suffix = Path(name).suffix.lower()
            fmt = suffix.lstrip(".")
            if fmt not in FORMATS:
                continue
            stem = name[:-len(suffix)]
            match = re.search(r"\s+\[(\d+)\]$", stem)
            if match:
                by_id.add((match.group(1), fmt))
            else:
                by_title.add((normalized_title(stem), fmt))
    return by_id, by_title


def missing_entries(entries, root):
    by_id, by_title = file_inventory(root)
    def title_keys(entry):
        return {(normalized_title(name), entry["format"])
                for name in (entry["title"], safe_name(entry["title"]))}

    title_counts = Counter(key for entry in entries for key in title_keys(entry))
    return [entry for entry in entries
            if (entry["book_id"], entry["format"]) not in by_id
            and not any(key in by_title and title_counts[key] == 1
                        for key in title_keys(entry))]


def _validate_payload(path, fmt, mime, cancel):
    with path.open("rb") as stream:
        head = stream.read(8192)
    if not head:
        raise ValueError("пустой ответ сервера")
    lowered = head.lstrip().lower()
    if lowered.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        raise ValueError("сервер вернул HTML вместо книги")
    if fmt == "epub" and mime in ("application/epub", "application/epub+zip"):
        if not zipfile.is_zipfile(path):
            raise ValueError("сервер вернул повреждённый EPUB")
        return ".epub", None
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            members = [item for item in archive.infolist()
                       if not item.is_dir() and Path(item.filename).suffix.lower() == "." + fmt]
            if not members:
                raise ValueError(f"в ZIP нет файла .{fmt}")
            if members[0].file_size > EXTRACT_LIMIT:
                raise ValueError("книга в ZIP превышает допустимый размер")
            # Reading the member verifies its CRC even when saving the ZIP itself.
            with archive.open(members[0]) as item:
                size = 0
                while chunk := item.read(64 * 1024):
                    if cancel.is_set():
                        raise DownloadCancelled()
                    size += len(chunk)
                    if size > EXTRACT_LIMIT:
                        raise ValueError("книга в ZIP превышает допустимый размер")
            return f".{fmt}.zip", members[0]
    if head.startswith(b"PK\x03\x04"):
        raise ValueError("сервер вернул повреждённый ZIP")
    if head.startswith(b"Rar!\x1a\x07"):
        return f".{fmt}.rar", None
    if fmt == "pdf" and not head.startswith(b"%PDF-"):
        raise ValueError("сервер вернул данные вместо PDF")
    if fmt == "djvu" and not head.startswith(b"AT&TFORM"):
        raise ValueError("сервер вернул данные вместо DJVU")
    if fmt == "fb2" and b"<fictionbook" not in lowered:
        raise ValueError("сервер вернул данные вместо FB2")
    if fmt == "rtf" and not head.startswith(b"{\\rtf"):
        raise ValueError("сервер вернул данные вместо RTF")
    return f".{fmt}", None


def _temporary(output):
    stream = tempfile.NamedTemporaryFile("w+b", dir=output, prefix=".flibusta-book-",
                                         suffix=".tmp", delete=False)
    return stream, Path(stream.name)


def download_book(entry, output, transport, extract_zip=False):
    """Stream one file to disk, validate it, then publish its final name."""
    temporary = extracted = None
    try:
        stream, temporary = _temporary(output)
        with stream:
            transport.fetch(entry["url"], stream, BOOK_LIMIT)
        suffix, member = _validate_payload(temporary, entry["format"], entry["mime"],
                                           transport.cancel)
        source = temporary
        if member is not None and extract_zip:
            if member.file_size > EXTRACT_LIMIT:
                raise ValueError("книга в ZIP превышает допустимый размер")
            stream, extracted = _temporary(output)
            with stream, zipfile.ZipFile(temporary) as archive, archive.open(member) as item:
                size = 0
                while chunk := item.read(64 * 1024):
                    if transport.cancel.is_set():
                        raise DownloadCancelled()
                    size += len(chunk)
                    if size > EXTRACT_LIMIT:
                        raise ValueError("книга в ZIP превышает допустимый размер")
                    stream.write(chunk)
            source = extracted
            _validate_payload(source, entry["format"], "", transport.cancel)
            suffix = f".{entry['format']}"
        target = output / f"{safe_name(entry['title'])} [{entry['book_id']}]{suffix}"
        if transport.cancel.is_set():
            raise DownloadCancelled()
        try:
            os.link(source, target)
        except FileExistsError:
            return False, target
        return True, target
    finally:
        for path in (temporary, extracted):
            if path is not None:
                path.unlink(missing_ok=True)
