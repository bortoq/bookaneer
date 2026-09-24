#!/usr/bin/env python3
"""Download an author's books from a Flibusta-compatible OPDS catalog."""

import argparse
import configparser
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import io
from pathlib import Path
import re
import sys
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
import zipfile

ATOM = "{http://www.w3.org/2005/Atom}"
FORMATS = {"fb2", "djvu", "pdf", "epub", "mobi", "txt", "rtf", "html"}
MIME_FORMATS = {
    "application/fb2+zip": "fb2", "application/djvu": "djvu",
    "image/vnd.djvu": "djvu", "application/pdf": "pdf",
    "application/pdf+zip": "pdf", "application/pdf+rar": "pdf",
    "application/epub+zip": "epub", "application/epub": "epub",
    "application/x-mobipocket-ebook": "mobi", "application/txt+zip": "txt",
    "application/rtf+zip": "rtf", "application/html+zip": "html",
}
USER_AGENT = "flibusta-author-downloader/1.0"


@dataclass(frozen=True)
class Book:
    id: str
    title: str
    language: str
    original_format: str
    downloads: tuple[tuple[str, str], ...]


class AuthorLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            match = re.search(r"(?:^|/)a/(\d+)(?:[/?#]|$)", href)
            if match:
                self.current = [match.group(1), ""]

    def handle_data(self, data):
        if self.current is not None:
            self.current[1] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            self.links.append(tuple(self.current))
            self.current = None


def get(url):
    with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=30) as response:
        return response.read(), response.headers, response.url


def author_id_and_base(value, mirror):
    if re.fullmatch(r"\d+", value):
        return value, mirror.rstrip("/") + "/"
    parsed = urlparse(value)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        match = re.fullmatch(r"/a/(\d+)/?", parsed.path)
        if not match:
            raise ValueError("Ссылка должна иметь вид https://домен/a/2583")
        return match.group(1), f"{parsed.scheme}://{parsed.netloc}/"
    base = mirror.rstrip("/") + "/"
    url = urljoin(base, "booksearch?" + urlencode({"ask": value, "cha": "on"}))
    page, _, _ = get(url)
    parser = AuthorLinks()
    parser.feed(page.decode("utf-8", errors="replace"))
    found = list(dict.fromkeys((ident, unescape(name).strip()) for ident, name in parser.links))
    exact = [item for item in found if item[1].casefold() == value.strip().casefold()]
    if len(exact) == 1:
        return exact[0][0], base
    if not found:
        raise ValueError(f"Автор «{value}» не найден")
    matches = exact or found
    raise ValueError("Уточните автора ссылкой /a/ID: " + "; ".join(f"{name} ({ident})" for ident, name in matches[:10]))


def description_field(content, label):
    text = unescape(unescape(content or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    match = re.search(rf"(?:^|\s){re.escape(label)}\s*:\s*([^\s<]+)", text, re.I)
    return match.group(1).strip().lower() if match else ""


def parse_feed(data):
    root = ET.fromstring(data)
    if root.tag != ATOM + "feed":
        raise ValueError("Сервер не вернул каталог OPDS")
    books = []
    for entry in root.findall(ATOM + "entry"):
        content = entry.findtext(ATOM + "content", default="")
        language = (entry.findtext("{http://purl.org/dc/terms/}language") or
                    entry.findtext(ATOM + "language") or description_field(content, "Язык")).lower()
        original = description_field(content, "Формат")
        downloads = []
        for link in entry.findall(ATOM + "link"):
            href = link.get("href", "")
            mime = link.get("type", "").lower()
            if href and (link.get("rel", "").startswith("http://opds-spec.org/acquisition") or
                         mime in MIME_FORMATS):
                downloads.append((href, mime))
        ident = entry.findtext(ATOM + "id", default="")
        match = re.search(r"/b/(\d+)", ident)
        if not match:
            for href, _ in downloads:
                match = re.search(r"/b/(\d+)", href)
                if match:
                    break
        if match:
            books.append(Book(match.group(1), entry.findtext(ATOM + "title", default=match.group(1)),
                              language, original, tuple(downloads)))
    next_url = next((link.get("href") for link in root.findall(ATOM + "link")
                     if link.get("rel") == "next"), None)
    return books, next_url


def iter_books(base, author_id):
    url = urljoin(base, f"opds/author/{author_id}/alphabet/0")
    seen_pages = set()
    while url and url not in seen_pages:
        seen_pages.add(url)
        data, _, actual_url = get(url)
        books, next_href = parse_feed(data)
        yield from books
        url = urljoin(actual_url, next_href) if next_href else None


def safe_name(title):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")
    return name[:100] or "Книга"


def download_book(base, book, fmt, href, mime, output):
    url = urljoin(base, href)
    data, headers, _ = get(url)
    if not data:
        raise ValueError("пустой ответ сервера")
    stem = f"{safe_name(book.title)} [{book.id}]"
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [item for item in archive.infolist()
                       if not item.is_dir() and Path(item.filename).suffix.lower() == "." + fmt]
            if not members:
                raise ValueError(f"в ZIP нет файла .{fmt}")
            data = archive.read(members[0])
        suffix = "." + fmt
    elif data.startswith(b"Rar!\x1a\x07"):
        suffix = f".{fmt}.rar"
    elif data.lstrip().lower().startswith(b"<!doctype html") or data.lstrip().lower().startswith(b"<html"):
        raise ValueError("сервер вернул HTML вместо книги")
    else:
        suffix = "." + fmt
    target = output / (stem + suffix)
    if target.exists():
        return False, target
    # Exclusive create preserves existing files even if a second process is running.
    with target.open("xb") as stream:
        stream.write(data)
    return True, target


def settings(path):
    config = configparser.ConfigParser()
    if not config.read(path, encoding="utf-8"):
        raise ValueError(f"Не найден файл настроек: {path}")
    return (config.get("defaults", "languages", fallback="ru").split(),
            config.get("defaults", "formats", fallback="fb2").split(),
            config.get("site", "mirror", fallback="https://flibusta.is"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Скачать книги автора из Флибусты")
    parser.add_argument("author", help="ссылка /a/ID, ID или точное имя автора")
    parser.add_argument("-l", "--languages", nargs="+", metavar="LANG", help="языки книг")
    parser.add_argument("-f", "--formats", nargs="+", metavar="FORMAT", help="форматы книг")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("flibusta.ini"))
    args = parser.parse_args(argv)
    try:
        default_languages, default_formats, mirror = settings(args.config)
        languages = {value.lower() for value in (args.languages or default_languages)}
        formats = list(dict.fromkeys(value.lower() for value in (args.formats or default_formats)))
        if not languages or not formats or any(fmt not in FORMATS for fmt in formats):
            raise ValueError("Укажите языки и форматы; форматы: " + ", ".join(sorted(FORMATS)))
        author_id, base = author_id_and_base(args.author, mirror)
        count = skipped = failed = 0
        seen = set()
        for book in iter_books(base, author_id):
            if book.language not in languages:
                continue
            for fmt in formats:
                key = (book.id, fmt)
                if key in seen:
                    continue
                seen.add(key)
                candidates = [(href, mime) for href, mime in book.downloads
                              if MIME_FORMATS.get(mime) == fmt or
                              (href.rstrip("/").endswith("/download") and book.original_format == fmt)]
                if not candidates:
                    continue
                href, mime = candidates[0]
                try:
                    created, target = download_book(base, book, fmt, href, mime, Path.cwd())
                    print(("Сохранено: " if created else "Уже есть: ") + target.name)
                    count += created
                    skipped += not created
                except Exception as exc:
                    failed += 1
                    print(f"Ошибка: {book.title} [{book.id}] {fmt}: {exc}", file=sys.stderr)
        print(f"Готово: скачано {count}, уже было {skipped}, ошибок {failed}")
        return 1 if failed else 0
    except (OSError, ValueError, ET.ParseError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
