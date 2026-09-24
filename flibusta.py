#!/usr/bin/env python3
"""Download an author's books from a Flibusta-compatible OPDS catalog."""

import argparse
from collections import Counter
import configparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import unicodedata
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.error import HTTPError, URLError
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
LOG_NAME = "flibusta-downloads.json"
_cancel_requested = threading.Event()


class DownloadCancelled(Exception):
    pass


class AmbiguousAuthors(Exception):
    def __init__(self, authors, base):
        self.authors = authors
        self.base = base


class Spinner:
    FRAMES = (" ", "░", "▒", "▓", "█", "▓", "▒", "░")

    def __init__(self, action):
        self.action = action
        self.stream = sys.stderr
        # MC's subshell shares a terminal with its panel renderer. Redrawing
        # the same line there can disturb its Ctrl-O screen and prompt state.
        self.animated = (self.stream.isatty()
                         and not (os.environ.get("MC_SID") or os.environ.get("MC_TMPDIR")))
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.thread = None

    def __enter__(self):
        if self.animated:
            self.thread = threading.Thread(target=self.animate, daemon=True)
            self.thread.start()
        else:
            print(self.action, file=self.stream, flush=True)
        return self

    def animate(self):
        index = 0
        while not self.stop.is_set():
            with self.lock:
                self.stream.write(f"\r\033[2K{self.FRAMES[index % len(self.FRAMES)]} {self.action}")
                self.stream.flush()
            index += 1
            self.stop.wait(0.12)

    def report(self, message, stream=None):
        with self.lock:
            if self.animated:
                self.stream.write("\r\033[2K")
                self.stream.flush()
            print(message, file=stream or sys.stdout, flush=True)

    def __exit__(self, *_):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
        if self.animated:
            with self.lock:
                self.stream.write("\r\033[2K\n")
                self.stream.flush()


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
        self.pages = set()
        self.current = None
        self.li_links = None
        self.li_text = ""
        self.aliases = {}

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.li_links = []
            self.li_text = ""
        if tag == "a":
            href = dict(attrs).get("href", "")
            parsed = urlparse(href)
            if parsed.path.endswith("booksearch"):
                page = parse_qs(parsed.query).get("page", [""])[0]
                if page.isdecimal():
                    self.pages.add(int(page))
            match = re.search(r"(?:^|/)a/(\d+)(?:[/?#]|$)", href)
            if match:
                self.current = [match.group(1), ""]

    def handle_data(self, data):
        if self.li_links is not None:
            self.li_text += data
        if self.current is not None:
            self.current[1] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            if self.li_links is None:
                self.links.append(tuple(self.current))
            else:
                self.li_links.append(tuple(self.current))
            self.current = None
        if tag == "li" and self.li_links is not None:
            if "через синоним" in self.li_text.casefold() and self.li_links:
                primary = self.li_links[0]
                self.links.append(primary)
                self.aliases.setdefault(primary[0], []).extend(name for _, name in self.li_links[1:])
            else:
                self.links.extend(self.li_links)
            self.li_links = None


def get(url):
    for attempt in range(3):
        if _cancel_requested.is_set():
            raise DownloadCancelled()
        try:
            with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=30) as response:
                chunks = []
                while True:
                    if _cancel_requested.is_set():
                        raise DownloadCancelled()
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        return b"".join(chunks), response.headers, response.url
                    chunks.append(chunk)
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
        if _cancel_requested.wait(attempt + 1):
            raise DownloadCancelled()


def find_authors_html(name, base):
    """Return all author results, including search result pages."""
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
        page, _, _ = get(url)
        parser = AuthorLinks()
        parser.feed(page.decode("utf-8", errors="replace"))
        for ident, author_name in parser.links:
            found.setdefault(ident, unescape(author_name).strip())
        for ident, names in parser.aliases.items():
            aliases.setdefault(ident, set()).update(unescape(alias).strip() for alias in names)
        pending.extend(sorted(parser.pages - visited - set(pending)))
    return [(ident, author_name, aliases.get(ident, set())) for ident, author_name in found.items()]


def find_authors_opds(name, base):
    found = {}
    url = urljoin(base, "opds/search?" + urlencode({"searchType": "authors", "searchTerm": name}))
    visited = set()
    while url and url not in visited:
        visited.add(url)
        data, _, actual_url = get(url)
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


def find_authors(name, base):
    try:
        return find_authors_html(name, base), base
    except (HTTPError, URLError, TimeoutError) as first_error:
        bases = [base]
        if urlparse(base).hostname == "flibusta.is":
            bases.append("https://flub.flibusta.is/")
        failures = [f"{base}booksearch: {first_error}"]
        for opds_base in bases:
            try:
                return find_authors_opds(name, opds_base), opds_base
            except (HTTPError, URLError, TimeoutError, ET.ParseError, ValueError) as exc:
                failures.append(f"{opds_base}opds/search: {exc}")
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
    found, base = find_authors(value, base)
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


def download_book(base, book, fmt, href, mime, output, extract_zip=False):
    url = urljoin(base, href)
    data, headers, _ = get(url)
    if _cancel_requested.is_set():
        raise DownloadCancelled()
    if not data:
        raise ValueError("пустой ответ сервера")
    if data.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        raise ValueError("сервер вернул HTML вместо книги")
    stem = f"{safe_name(book.title)} [{book.id}]"
    if fmt == "epub" and mime in ("application/epub", "application/epub+zip"):
        suffix = ".epub"
    elif zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [item for item in archive.infolist()
                       if not item.is_dir() and Path(item.filename).suffix.lower() == "." + fmt]
            if not members:
                raise ValueError(f"в ZIP нет файла .{fmt}")
            if extract_zip:
                data = archive.read(members[0])
        suffix = "." + fmt if extract_zip else f".{fmt}.zip"
    elif data.startswith(b"Rar!\x1a\x07"):
        suffix = f".{fmt}.rar"
    else:
        suffix = "." + fmt
    target = output / (stem + suffix)
    if target.exists():
        return False, target
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=output, prefix=".flibusta-book-",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        # Hard link creates the final name only after the complete file is written.
        if _cancel_requested.is_set():
            raise DownloadCancelled()
        try:
            os.link(temporary, target)
        except FileExistsError:
            return False, target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True, target


def settings(path):
    config = configparser.ConfigParser()
    if not config.read(path, encoding="utf-8"):
        raise ValueError(f"Не найден файл настроек: {path}")
    workers = config.getint("downloads", "workers", fallback=4)
    if not 1 <= workers <= 16:
        raise ValueError("[downloads] workers должен быть от 1 до 16")
    return (config.get("defaults", "languages", fallback="ru").split(),
            config.get("defaults", "formats", fallback="fb2").split(),
            config.get("site", "mirror", fallback="https://flibusta.is"), workers,
            config.getboolean("downloads", "extract_zip", fallback=False))


def load_log(path):
    if not path.exists():
        return {"version": 1, "entries": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("entries"), dict):
        raise ValueError(f"Неверный формат журнала: {path}")
    return data


def save_log(path, log):
    # Replace atomically so an interrupted run leaves the previous log intact.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".flibusta-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(log, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def download_from_entry(entry, output):
    book = Book(entry["book_id"], entry["title"], entry["language"], "", ())
    return download_book(entry["url"], book, entry["format"],
                         entry["url"], entry["mime"], output, entry.get("extract_zip", False))


def run_downloads(entries, log, log_path, output, workers, action):
    if not entries:
        print("Книг для загрузки нет", file=sys.stderr)
        return 0
    for entry in entries:
        entry["status"] = "pending"
        entry["attempts"] = entry.get("attempts", 0) + 1
        entry["error"] = None
    if log is not None:
        save_log(log_path, log)
    downloaded = existing = failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        with Spinner(f"{action} ({len(entries)})") as spinner:
            try:
                for entry in entries:
                    futures[pool.submit(download_from_entry, entry, output)] = entry
                for future in as_completed(futures):
                    entry = futures[future]
                    try:
                        created, target = future.result()
                        entry["status"] = "downloaded" if created else "existing"
                        entry["file"] = target.name
                        downloaded += created
                        existing += not created
                        if created:
                            spinner.report(target.name)
                    except Exception as exc:
                        entry["status"] = "failed"
                        entry["error"] = str(exc)
                        failed += 1
                        spinner.report(f"Ошибка: {entry['title']} [{entry['book_id']}] {entry['format']}: {exc}",
                                       sys.stderr)
                    if log is not None:
                        save_log(log_path, log)
            except KeyboardInterrupt:
                _cancel_requested.set()
                for future in futures:
                    future.cancel()
                spinner.report("Останавливаю загрузки...", sys.stderr)
                raise
    print(f"Готово: скачано {downloaded}, уже было {existing}, ошибок {failed}", file=sys.stderr)
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Скачать книги автора из Флибусты")
    parser.add_argument("author", nargs="?", help="ссылка /a/ID, ID или имя автора")
    parser.add_argument("-a", "--search-author", dest="search_author", metavar="NAME",
                        help="найти автора по имени и скачать его книги")
    parser.add_argument("-r", "--retry", action="store_true",
                        help="повторить неудачные и прерванные загрузки из журнала")
    parser.add_argument("-s", "--sync", action="store_true",
                        help="докачать отсутствующие книги по файлам в текущей папке и подпапках")
    parser.add_argument("-x", "--extract", action="store_true", default=None,
                        help="распаковывать скачанные ZIP вместо сохранения архивов")
    parser.add_argument("-l", "--languages", nargs="+", metavar="LANG", help="языки книг")
    parser.add_argument("-f", "--formats", nargs="+", metavar="FORMAT", help="форматы книг")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("flibusta.ini"))
    args = parser.parse_args(argv)
    if args.retry and args.sync:
        parser.error("-s и -r нельзя использовать вместе")
    if args.retry:
        if args.author or args.search_author or args.languages or args.formats:
            parser.error("-r запускается отдельно, без автора, -l и -f")
    elif bool(args.author) == bool(args.search_author):
        parser.error("укажите ссылку/ID автора или -a ИМЯ")
    _cancel_requested.clear()
    try:
        log_path = Path.cwd() / LOG_NAME
        if args.retry and not log_path.exists():
            raise ValueError(f"Журнал загрузок не найден: {log_path}")
        try:
            log = load_log(log_path)
        except (ValueError, TypeError, json.JSONDecodeError):
            if not args.sync:
                raise
            log = None
        default_languages, default_formats, mirror, workers, default_extract = settings(args.config)
        extract_zip = args.extract if args.extract is not None else default_extract
        if args.retry:
            entries = [entry for entry in log["entries"].values()
                       if entry["status"] in ("failed", "pending")]
            for entry in entries:
                entry["extract_zip"] = (True if args.extract else entry.get("extract_zip", default_extract))
            return run_downloads(entries, log, log_path, Path.cwd(), workers, "Повторяю загрузку")
        languages = {value.lower() for value in (args.languages or default_languages)}
        formats = list(dict.fromkeys(value.lower() for value in (args.formats or default_formats)))
        if not languages or not formats or any(fmt not in FORMATS for fmt in formats):
            raise ValueError("Укажите языки и форматы; форматы: " + ", ".join(sorted(FORMATS)))
        with Spinner("Ищу автора" if args.search_author else "Определяю автора"):
            author_id, base = author_id_and_base(args.search_author or args.author, mirror)
        seen = set()
        entries = []
        with Spinner("Читаю каталог автора"):
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
                    url = urljoin(base, href)
                    entry = (log["entries"].setdefault(f"{url}|{fmt}", {}) if log is not None else {})
                    entry.update({"book_id": book.id, "title": book.title,
                                  "language": book.language, "format": fmt,
                                  "url": url, "mime": mime, "extract_zip": extract_zip})
                    entries.append(entry)
        if args.sync:
            with Spinner("Проверяю файлы в папках"):
                missing = missing_entries(entries, Path.cwd())
                missing_ids = {id(entry) for entry in missing}
                if log is not None:
                    changed = False
                    for entry in entries:
                        if id(entry) not in missing_ids and entry.get("status") in ("failed", "pending"):
                            entry["status"] = "existing"
                            entry["error"] = None
                            changed = True
                    if changed:
                        save_log(log_path, log)
                entries = missing
        return run_downloads(entries, log, log_path, Path.cwd(), workers,
                             "Синхронизирую книги" if args.sync else "Скачиваю книги")
    except AmbiguousAuthors as exc:
        for ident, name in exc.authors:
            print(f"{name}: {urljoin(exc.base, f'a/{ident}')}")
        return 1
    except KeyboardInterrupt:
        _cancel_requested.set()
        print("Прервано пользователем", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, ET.ParseError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
