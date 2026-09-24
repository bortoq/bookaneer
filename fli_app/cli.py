"""Command line orchestration."""

import argparse
import configparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import sqlite3
import sys
import threading
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

import requests

from .catalog import AmbiguousAuthors, FORMATS, MIME_FORMATS, author_id_and_base, iter_books
from .files import download_book, missing_entries
from .journal import Journal, LOG_NAME
from .transport import DownloadCancelled, Transport
from .ui import Spinner


def settings(path):
    config = configparser.ConfigParser(interpolation=None)
    if not config.read(path, encoding="utf-8"):
        raise ValueError(f"Не найден файл настроек: {path}")
    workers = config.getint("downloads", "workers", fallback=4)
    if not 1 <= workers <= 16:
        raise ValueError("[downloads] workers должен быть от 1 до 16")
    return (config.get("defaults", "languages", fallback="ru").split(),
            config.get("defaults", "formats", fallback="fb2").split(),
            config.get("site", "mirror", fallback="https://flibusta.is"), workers,
            config.getboolean("downloads", "extract_zip", fallback=False))


def run_downloads(jobs, journal, output, workers, transport, action):
    if not jobs:
        print("Книг для загрузки нет", file=sys.stderr)
        return 0
    downloaded = existing = failed = 0
    futures = {}
    processed = set()
    if journal:
        journal.queue(jobs)

    def finish(future, spinner=None):
        nonlocal downloaded, existing, failed
        if future in processed or future.cancelled():
            return
        processed.add(future)
        job = futures[future]
        try:
            created, target = future.result()
        except DownloadCancelled:
            return  # Keep pending for -r.
        except Exception as exc:
            failed += 1
            if journal:
                journal.finish(job, "failed", error=str(exc))
            message = f"Ошибка: {job['title']} [{job['book_id']}] {job['format']}: {exc}"
            if spinner:
                spinner.report(message, sys.stderr)
            else:
                print(message, file=sys.stderr)
        else:
            downloaded += created
            existing += not created
            if journal:
                journal.finish(job, "downloaded" if created else "existing", file=target.name)
            if created:
                if spinner:
                    spinner.report(target.name)
                else:
                    print(target.name)

    interrupted = False
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            with Spinner(f"{action} ({len(jobs)})") as spinner:
                try:
                    for job in jobs:
                        if transport.cancel.is_set():
                            raise KeyboardInterrupt()
                        if journal:
                            journal.mark_pending(job)
                        futures[pool.submit(download_book, job, output, transport,
                                            bool(job["extract_zip"]))] = job
                    for future in as_completed(futures):
                        finish(future, spinner)
                except KeyboardInterrupt:
                    transport.cancel.set()
                    for future in futures:
                        future.cancel()
                    spinner.report("Останавливаю загрузки...", sys.stderr)
                    interrupted = True
    finally:
        # The executor has stopped; record successful jobs that finished during shutdown.
        for future in futures:
            if future.done() and not future.cancelled():
                finish(future)
    if interrupted:
        raise KeyboardInterrupt()
    print(f"Готово: скачано {downloaded}, уже было {existing}, ошибок {failed}", file=sys.stderr)
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Скачать книги автора из Флибусты")
    parser.add_argument("author", nargs="?", help="ссылка /a/ID или ID автора")
    parser.add_argument("-a", "--search-author", metavar="NAME", help="найти автора по имени")
    parser.add_argument("-r", "--retry", action="store_true", help="повторить незавершённые загрузки")
    parser.add_argument("-s", "--sync", action="store_true", help="докачать отсутствующие книги")
    parser.add_argument("-x", "--extract", action="store_true", default=None,
                        help="распаковывать скачанные ZIP")
    parser.add_argument("-l", "--languages", nargs="+", metavar="LANG", help="языки книг")
    parser.add_argument("-f", "--formats", nargs="+", metavar="FORMAT", help="форматы книг")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "flibusta.ini")
    args = parser.parse_args(argv)
    if args.retry and args.sync:
        parser.error("-s и -r нельзя использовать вместе")
    if args.retry:
        if args.author or args.search_author or args.languages or args.formats:
            parser.error("-r запускается отдельно, без автора, -l и -f")
    elif bool(args.author) == bool(args.search_author):
        parser.error("укажите ссылку/ID автора или -a ИМЯ")

    cancel = threading.Event()
    transport = Transport(cancel)
    journal = None
    try:
        output = Path.cwd()
        log_path = output / LOG_NAME
        if args.retry and not log_path.exists():
            raise ValueError(f"Журнал загрузок не найден: {log_path}")
        default_languages, default_formats, mirror, workers, default_extract = settings(args.config)
        extract_zip = args.extract if args.extract is not None else default_extract
        if args.retry:
            journal = Journal(log_path)
            jobs = journal.retry_jobs()
            if args.extract:
                for job in jobs:
                    job["extract_zip"] = True
                    journal.upsert(job)
            return run_downloads(jobs, journal, output, workers, transport, "Повторяю загрузку")

        languages = {value.lower() for value in (args.languages or default_languages)}
        formats = list(dict.fromkeys(value.lower() for value in (args.formats or default_formats)))
        if not languages or not formats or any(fmt not in FORMATS for fmt in formats):
            raise ValueError("Укажите языки и форматы; форматы: " + ", ".join(sorted(FORMATS)))
        with Spinner("Ищу автора" if args.search_author else "Определяю автора"):
            author_id, base = author_id_and_base(args.search_author or args.author, mirror, transport)
        jobs = []
        seen = set()
        with Spinner("Читаю каталог автора"):
            for book in iter_books(base, author_id, transport):
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
                    jobs.append({"book_id": book.id, "title": book.title,
                                 "language": book.language, "format": fmt,
                                 "url": urljoin(base, href), "mime": mime,
                                 "extract_zip": int(extract_zip)})
        if jobs or log_path.exists():
            try:
                journal = Journal(log_path)
            except sqlite3.DatabaseError:
                if not args.sync:
                    raise
                journal = None
        if args.sync:
            with Spinner("Проверяю файлы в папках"):
                missing = missing_entries(jobs, output)
                missing_ids = {id(job) for job in missing}
                if journal:
                    for job in jobs:
                        if id(job) not in missing_ids and journal.status(job) in ("failed", "pending"):
                            journal.finish(job, "existing")
                jobs = missing
        if journal:
            for job in jobs:
                journal.upsert(job)
        return run_downloads(jobs, journal, output, workers, transport,
                             "Синхронизирую книги" if args.sync else "Скачиваю книги")
    except AmbiguousAuthors as exc:
        for ident, name in exc.authors:
            print(f"{name}: {urljoin(exc.base, f'a/{ident}')}")
        return 1
    except KeyboardInterrupt:
        cancel.set()
        print("Прервано пользователем", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError,
            sqlite3.DatabaseError, requests.RequestException) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    finally:
        if journal:
            journal.close()
