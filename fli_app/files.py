"""Local book inventory and file downloads."""

from collections import Counter
import os
from pathlib import Path
import re
import tempfile
import unicodedata
import zipfile
import xml.etree.ElementTree as ET

from .transport import DownloadCancelled

BOOK_LIMIT = 512 * 1024 * 1024
EXTRACT_LIMIT = 1024 * 1024 * 1024
ZIP_DOCUMENT_FORMATS = {"epub", "docx", "odt", "fb3", "cbz", "zip"}
ZIP_MEMBER_EXTENSIONS = {"djvu": {"djvu", "djv"}, "html": {"html", "htm"},
                         "jpg": {"jpg", "jpeg"}}
ZIP_METADATA_LIMIT = 8 * 1024 * 1024


def _zip_part(archive, name):
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise ValueError(f"в архиве нет обязательного файла {name}") from exc
    if info.file_size > ZIP_METADATA_LIMIT:
        raise ValueError(f"слишком большой служебный файл {name}")
    return archive.read(info)


def _zip_xml(archive, name):
    try:
        return ET.fromstring(_zip_part(archive, name))
    except ET.ParseError as exc:
        raise ValueError(f"повреждён XML-файл {name}") from exc


def _validate_document_zip(archive, fmt):
    if fmt == "docx":
        types = _zip_xml(archive, "[Content_Types].xml")
        relationships = _zip_xml(archive, "_rels/.rels")
        document = _zip_xml(archive, "word/document.xml")
        if (types.tag != "{http://schemas.openxmlformats.org/package/2006/content-types}Types"
                or relationships.tag != "{http://schemas.openxmlformats.org/package/2006/relationships}Relationships"
                or document.tag != "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document"):
            raise ValueError("неверная структура DOCX")
    elif fmt == "epub":
        if _zip_part(archive, "mimetype") != b"application/epub+zip":
            raise ValueError("неверный mimetype EPUB")
        container = _zip_xml(archive, "META-INF/container.xml")
        rootfiles = container.findall(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
        if not rootfiles:
            raise ValueError("в EPUB нет файла пакета из container.xml")
        package = _zip_xml(archive, rootfiles[0].get("full-path") or "")
        if package.tag != "{http://www.idpf.org/2007/opf}package":
            raise ValueError("неверный файл пакета EPUB")
    elif fmt == "odt":
        if _zip_part(archive, "mimetype") != b"application/vnd.oasis.opendocument.text":
            raise ValueError("неверный mimetype ODT")
        _zip_xml(archive, "content.xml")
        _zip_xml(archive, "META-INF/manifest.xml")
    elif fmt == "cbz":
        if not any(item.filename.casefold().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))
                   for item in archive.infolist() if not item.is_dir()):
            raise ValueError("в CBZ нет изображений")
    elif fmt == "fb3":
        _zip_xml(archive, "[Content_Types].xml")
        _zip_xml(archive, "_rels/.rels")
        _zip_xml(archive, "fb3/body.xml")
        _zip_xml(archive, "fb3/description.xml")

def safe_name(title):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")
    return name[:100] or "Книга"


def normalized_title(title):
    text = unicodedata.normalize("NFKC", title).casefold().replace("ё", "е")
    return " ".join(re.findall(r"\w+", text.replace("_", " ")))


def file_inventory(root, formats):
    """Index book files in root and all its subdirectories by ID or title."""
    by_id = set()
    by_title = set()
    suffixes = sorted(((suffix, fmt) for fmt in formats
                       for suffix in (f".{fmt}.zip", f".{fmt}.rar", f".{fmt}")),
                      key=lambda item: len(item[0]), reverse=True)
    for directory, subdirs, filenames in os.walk(root):
        subdirs[:] = [name for name in subdirs if name != ".git"]
        for filename in filenames:
            path = Path(directory) / filename
            if not path.is_file() or path.stat().st_size == 0:
                continue
            match_suffix = next(((suffix, fmt) for suffix, fmt in suffixes
                                 if filename.casefold().endswith(suffix)), None)
            if match_suffix is None:
                continue
            suffix, fmt = match_suffix
            stem = filename[:-len(suffix)]
            match = re.search(r"\s+\[(\d+)\]$", stem)
            if match:
                by_id.add((match.group(1), fmt))
            else:
                by_title.add((normalized_title(stem), fmt))
    return by_id, by_title


def missing_entries(entries, root):
    by_id, by_title = file_inventory(root, {entry["format"] for entry in entries})
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
    if fmt not in {"html", "htm", "mht", "mhtm"} and lowered.startswith(
            (b"<!doctype html", b"<html", b"<head", b"<body")):
        raise ValueError("сервер вернул HTML вместо книги")
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            if fmt in ZIP_DOCUMENT_FORMATS:
                _validate_document_zip(archive, fmt)
                return f".{fmt}", None
            extensions = ZIP_MEMBER_EXTENSIONS.get(fmt, {fmt})
            members = [item for item in archive.infolist()
                       if not item.is_dir() and any(item.filename.casefold().endswith("." + ext)
                                                    for ext in extensions)]
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
    if fmt in ZIP_DOCUMENT_FORMATS:
        raise ValueError(f"сервер вернул повреждённый {fmt.upper()}")
    if head.startswith(b"Rar!\x1a\x07"):
        return (f".{fmt}" if fmt == "cbr" else f".{fmt}.rar"), None
    if fmt == "pdf" and not head.startswith(b"%PDF-"):
        raise ValueError("сервер вернул данные вместо PDF")
    if fmt == "djvu" and not head.startswith(b"AT&TFORM"):
        raise ValueError("сервер вернул данные вместо DJVU")
    if fmt == "fb2" and b"<fictionbook" not in lowered:
        raise ValueError("сервер вернул данные вместо FB2")
    if fmt == "rtf" and not head.startswith(b"{\\rtf"):
        raise ValueError("сервер вернул данные вместо RTF")
    if fmt in {"azw3", "mobi"} and head[60:68] != b"BOOKMOBI":
        raise ValueError(f"сервер вернул данные вместо {fmt.upper()}")
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
