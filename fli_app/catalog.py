"""Author search and OPDS catalog parsing."""

from dataclasses import dataclass
from html import unescape
import re
import unicodedata
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

ATOM = "{http://www.w3.org/2005/Atom}"
MIME_FORMATS = {
    "application/fb2+zip": "fb2", "application/djvu": "djvu",
    "application/djvu+zip": "djvu", "image/vnd.djvu": "djvu",
    "application/pdf": "pdf",
    "application/pdf+zip": "pdf", "application/pdf+rar": "pdf",
    "application/epub+zip": "epub", "application/epub": "epub",
    "application/x-mobipocket-ebook": "mobi", "application/txt+zip": "txt",
    "application/rtf+zip": "rtf", "application/html+zip": "html",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.oasis.opendocument.text": "odt",
    "image/jpeg": "jpeg", "audio/mpeg": "mp3",
}


class AmbiguousAuthors(Exception):
    def __init__(self, authors, base):
        self.authors = authors
        self.base = base


@dataclass(frozen=True)
class Book:
    id: str
    title: str
    language: str
    original_format: str
    downloads: tuple[tuple[str, str, str], ...]


def normalize_format(value):
    """Accept catalog format labels while keeping them safe as file suffixes."""
    value = value.strip().casefold()
    return value if re.fullmatch(r"\w[\w+.-]{0,30}", value) else None


def mime_format(mime):
    if mime in MIME_FORMATS:
        return MIME_FORMATS[mime]
    if mime in {"application/octet-stream", "application/binary", "application/x-download"}:
        return None
    if mime.startswith("application/"):
        return normalize_format(mime.removeprefix("application/").removesuffix("+zip"))
    return None


def link_format(href, mime, original):
    path = urlparse(href).path
    match = re.search(r"/b/\d+/([^/]+)$", path)
    if match and match.group(1).lower() == "download":
        return normalize_format(original) or mime_format(mime)
    if match:
        return normalize_format(match.group(1))
    return mime_format(mime)


def find_authors_html(name, base, transport):
    """Read only the author-results list, excluding page navigation and book links."""
    found = {}
    aliases = {}
    pending = [0]
    visited = set()
    while pending:
        page_number = pending.pop(0)
        if page_number in visited:
            continue
        visited.add(page_number)
        url = urljoin(base, "booksearch?" + urlencode({"ask": name, "page": page_number, "cha": "on"}))
        page, _, _ = transport.get_bytes(url)
        soup = BeautifulSoup(page, "html.parser")
        heading = next((item for item in soup.find_all("h3")
                        if "найденные писатели" in item.get_text(" ", strip=True).casefold()), None)
        if heading:
            results = heading.find_next_sibling("ul")
            if results:
                for item in results.find_all("li", recursive=False):
                    links = [(re.search(r"(?:^|/)a/(\d+)(?:[/?#]|$)", link.get("href", "")),
                              link.get_text(" ", strip=True))
                             for link in item.find_all("a", href=True)]
                    links = [(match.group(1), label) for match, label in links if match]
                    if not links:
                        continue
                    ident, author_name = links[0]
                    found.setdefault(ident, unescape(author_name).strip())
                    if "через синоним" in item.get_text(" ", strip=True).casefold():
                        aliases.setdefault(ident, set()).update(label for _, label in links[1:])
        pages = set()
        for link in soup.find_all("a", href=True):
            parsed = urlparse(link["href"])
            if parsed.path.endswith("booksearch"):
                page_value = parse_qs(parsed.query).get("page", [""])[0]
                if page_value.isdecimal():
                    pages.add(int(page_value))
        pending.extend(sorted(pages - visited - set(pending)))
    return [(ident, author_name, aliases.get(ident, set())) for ident, author_name in found.items()]


def find_authors_opds(name, base, transport):
    found = {}
    url = urljoin(base, "opds/search?" + urlencode({"searchType": "authors", "searchTerm": name}))
    visited = set()
    while url and url not in visited:
        visited.add(url)
        data, _, actual_url = transport.get_bytes(url)
        root = ET.fromstring(data)
        if root.tag != ATOM + "feed":
            raise ValueError("Сервер не вернул каталог OPDS")
        for entry in root.findall(ATOM + "entry"):
            for link in entry.findall(ATOM + "link"):
                match = re.search(r"/opds/author/(\d+)(?:[/?#]|$)", link.get("href", ""))
                if match:
                    found.setdefault(match.group(1), entry.findtext(ATOM + "title", default="").strip())
                    break
        next_href = next((link.get("href") for link in root.findall(ATOM + "link")
                          if link.get("rel") == "next"), None)
        url = urljoin(actual_url, next_href) if next_href else None
    return [(ident, author_name) for ident, author_name in found.items()]


def find_authors(name, base, transport):
    try:
        authors = find_authors_html(name, base, transport)
        if authors:
            return authors, base
        first_error = ValueError("веб-поиск не вернул авторов")
    except requests.RequestException as exc:
        first_error = exc
    bases = [base]
    if urlparse(base).hostname == "flibusta.is":
        bases.append("https://flub.flibusta.is/")
    failures = [f"{base}booksearch: {first_error}"]
    for opds_base in bases:
        try:
            authors = find_authors_opds(name, opds_base, transport)
            if authors:
                return authors, opds_base
        except (requests.RequestException, ET.ParseError, ValueError) as exc:
            failures.append(f"{opds_base}opds/search: {exc}")
    if isinstance(first_error, ValueError) and len(failures) == 1:
        return [], base
    raise ValueError("Поиск автора недоступен. " + "; ".join(failures)) from first_error


def matching_authors(query, authors):
    def words(value):
        value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
        return re.findall(r"\w+", value)

    def names(item):
        return (item[1], *(item[2] if len(item) > 2 else ()))

    requested = words(query)
    if not requested:
        return []
    exact = [item for item in authors if any(words(name) == requested for name in names(item))]
    if exact:
        return [(item[0], item[1]) for item in exact]
    return [(item[0], item[1]) for item in authors
            if any(all(word in words(name) for word in requested)
                   for name in names(item))]


def author_id_and_base(value, mirror, transport):
    if re.fullmatch(r"\d+", value):
        return value, mirror.rstrip("/") + "/"
    parsed = urlparse(value)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        match = re.fullmatch(r"/a/(\d+)/?", parsed.path)
        if not match:
            raise ValueError("Ссылка должна иметь вид https://домен/a/2583")
        return match.group(1), f"{parsed.scheme}://{parsed.netloc}/"
    base = mirror.rstrip("/") + "/"
    found, base = find_authors(value, base, transport)
    found = matching_authors(value, found)
    if not found:
        raise ValueError(f"Автор «{value}» не найден")
    if len(found) > 1:
        raise AmbiguousAuthors(found, base)
    return found[0][0], base


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
            mime = link.get("type", "").split(";", 1)[0].strip().lower()
            if href and link.get("rel", "").startswith("http://opds-spec.org/acquisition"):
                fmt = link_format(href, mime, original)
                if fmt:
                    downloads.append((href, mime, fmt))
        ident = entry.findtext(ATOM + "id", default="")
        match = re.search(r"/b/(\d+)", ident)
        if not match:
            for href, _, _ in downloads:
                match = re.search(r"/b/(\d+)", href)
                if match:
                    break
        if match:
            books.append(Book(match.group(1), entry.findtext(ATOM + "title", default=match.group(1)),
                              language, original, tuple(downloads)))
    next_url = next((link.get("href") for link in root.findall(ATOM + "link")
                     if link.get("rel") == "next"), None)
    return books, next_url


def iter_books(base, author_id, transport):
    url = urljoin(base, f"opds/author/{author_id}/alphabet/0")
    seen_pages = set()
    while url and url not in seen_pages:
        seen_pages.add(url)
        data, _, actual_url = transport.get_bytes(url)
        books, next_href = parse_feed(data)
        yield from books
        url = urljoin(actual_url, next_href) if next_href else None
