import io
import json
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import zipfile

import flibusta


FEED_0 = b'''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><id>/b/10</id><title>Russian Book</title>
    <content type="html">&#x3C;br/&#x3E;\xd0\xa4\xd0\xbe\xd1\x80\xd0\xbc\xd0\xb0\xd1\x82: fb2&#x3C;br/&#x3E;\xd0\xaf\xd0\xb7\xd1\x8b\xd0\xba: ru</content>
    <link rel="http://opds-spec.org/acquisition" href="/b/10/fb2" type="application/fb2+zip"/>
    <link rel="http://opds-spec.org/acquisition" href="/b/10/pdf" type="application/pdf"/>
  </entry>
  <link rel="next" href="/opds/author/1/alphabet/1"/>
</feed>'''
FEED_1 = b'''<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><id>/b/11</id><title>English Book</title>
    <content type="html">\xd0\xa4\xd0\xbe\xd1\x80\xd0\xbc\xd0\xb0\xd1\x82: pdf&amp;lt;br/&amp;gt;\xd0\xaf\xd0\xb7\xd1\x8b\xd0\xba: en</content>
    <link rel="http://opds-spec.org/acquisition" href="/b/11/download" type="application/pdf+rar"/>
  </entry>
</feed>'''


def archive():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as output:
        output.writestr("book.fb2", "book text")
    return data.getvalue()


class DownloaderTests(unittest.TestCase):
    def test_feed_pagination_and_formats(self):
        books, next_url = flibusta.parse_feed(FEED_0)
        self.assertEqual((books[0].language, books[0].original_format), ("ru", "fb2"))
        self.assertEqual(next_url, "/opds/author/1/alphabet/1")
        english, _ = flibusta.parse_feed(FEED_1)
        self.assertEqual((english[0].language, english[0].original_format), ("en", "pdf"))

    def test_cli_saves_zip_by_default_and_extracts_with_x(self):
        pages = {"/opds/author/1/alphabet/0": FEED_0,
                 "/opds/author/1/alphabet/1": FEED_1,
                 "/b/10/fb2": archive(), "/b/10/pdf": b"%PDF-1.4", "/b/11/download": b"Rar!\x1a\x07payload"}
        calls = []

        def fake_get(url):
            path = url.removeprefix("https://mirror.test")
            calls.append(path)
            return pages[path], {}, url

        with tempfile.TemporaryDirectory() as folder, patch.object(flibusta, "get", fake_get), patch.object(flibusta.Path, "cwd", return_value=Path(folder)):
            config = Path(folder) / "config.ini"
            config.write_text("[defaults]\nlanguages = ru\nformats = fb2\n[site]\nmirror = https://mirror.test\n")
            self.assertEqual(flibusta.main(["1", "--config", str(config)]), 0)
            self.assertEqual((Path(folder) / "Russian Book [10].fb2.zip").read_bytes(), pages["/b/10/fb2"])
            self.assertNotIn("/b/10/pdf", calls)
            self.assertEqual(flibusta.main(["1", "--config", str(config), "-l", "en", "-f", "pdf"]), 0)
            self.assertEqual((Path(folder) / "English Book [11].pdf.rar").read_bytes(), pages["/b/11/download"])
            self.assertEqual(flibusta.main(["1", "--config", str(config), "-x"]), 0)
            self.assertEqual((Path(folder) / "Russian Book [10].fb2").read_text(), "book text")

    def test_author_url(self):
        self.assertEqual(flibusta.author_id_and_base("http://flibusta.is/a/2583", "https://mirror.test"),
                         ("2583", "http://flibusta.is/"))

    def test_search_single_author(self):
        search = '<ul><li><a href="/a/2583">Альфонс Доде</a></li></ul>'.encode()
        calls = []

        def fake_get(url):
            calls.append(url)
            if "booksearch?" in url:
                return search, {}, url
            if "/opds/author/2583/" in url:
                return FEED_0.replace(b"/opds/author/1/alphabet/1", b""), {}, url
            return archive(), {}, url

        with tempfile.TemporaryDirectory() as folder, patch.object(flibusta, "get", fake_get), patch.object(flibusta.Path, "cwd", return_value=Path(folder)):
            config = Path(folder) / "config.ini"
            config.write_text("[defaults]\nlanguages = ru\nformats = fb2\n[site]\nmirror = https://mirror.test\n")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(flibusta.main(["-a", "альфонс доде", "--config", str(config)]), 0)
            self.assertTrue((Path(folder) / "Russian Book [10].fb2.zip").exists())
            self.assertTrue(any("/opds/author/2583/" in call for call in calls))

    def test_search_multiple_authors_prints_every_url_and_stops(self):
        pages = {
            "page=0": '<ul><li><a href="/a/2583">Альфонс Доде</a></li></ul><a href="/booksearch?ask=x&amp;page=1&amp;cha=on">2</a>',
            "page=1": '<ul><li><a href="/a/99">Жан Доде</a></li></ul>',
        }
        calls = []

        def fake_get(url):
            calls.append(url)
            return pages["page=1" if "page=1" in url else "page=0"].encode(), {}, url

        with tempfile.TemporaryDirectory() as folder, patch.object(flibusta, "get", fake_get):
            config = Path(folder) / "config.ini"
            config.write_text("[site]\nmirror = https://mirror.test\n")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(flibusta.main(["-a", "доде", "--config", str(config)]), 1)
            self.assertEqual(out.getvalue().splitlines(), [
                "Альфонс Доде: https://mirror.test/a/2583",
                "Жан Доде: https://mirror.test/a/99",
            ])
            self.assertNotIn("Ошибка:", err.getvalue())
            self.assertEqual(len(calls), 2)

    def test_retry_uses_log_without_refetching_catalog(self):
        feed = FEED_0.replace(b"/opds/author/1/alphabet/1", b"")
        attempts = []

        def fake_get(url):
            attempts.append(url)
            if "/opds/" in url:
                return feed, {}, url
            if len([item for item in attempts if "/b/10/fb2" in item]) == 1:
                raise TimeoutError("timed out")
            return archive(), {}, url

        with tempfile.TemporaryDirectory() as folder, patch.object(flibusta, "get", fake_get), patch.object(flibusta.Path, "cwd", return_value=Path(folder)):
            config = Path(folder) / "config.ini"
            config.write_text("[defaults]\nlanguages = ru\nformats = fb2\n[site]\nmirror = https://mirror.test\n")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(flibusta.main(["1", "--config", str(config), "-x"]), 1)
            log_path = Path(folder) / flibusta.LOG_NAME
            entry = next(iter(json.loads(log_path.read_text())["entries"].values()))
            self.assertEqual((entry["status"], entry["language"], entry["format"], entry["attempts"]),
                             ("failed", "ru", "fb2", 1))
            before = len([url for url in attempts if "/opds/" in url])
            with redirect_stdout(io.StringIO()):
                self.assertEqual(flibusta.main(["-r"]), 0)
            self.assertEqual(len([url for url in attempts if "/opds/" in url]), before)
            self.assertEqual((Path(folder) / "Russian Book [10].fb2").read_text(), "book text")
            entry = next(iter(json.loads(log_path.read_text())["entries"].values()))
            self.assertEqual((entry["status"], entry["attempts"]), ("downloaded", 2))
            count = len(attempts)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(flibusta.main(["-r"]), 0)
            self.assertEqual(len(attempts), count)

    def test_sync_requires_author(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            flibusta.main(["-s"])

    def test_author_search_falls_back_to_opds_after_503(self):
        author_feed = '''<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><title>Франсуа Рабле</title><link href="/opds/author/2583" /></entry>
        </feed>'''.encode()
        calls = []

        def fake_get(url):
            calls.append(url)
            if "booksearch" in url:
                raise HTTPError(url, 503, "Backend fetch failed", {}, None)
            if url.startswith("https://flibusta.is/"):
                raise HTTPError(url, 503, "Backend fetch failed", {}, None)
            return author_feed, {}, url

        with patch.object(flibusta, "get", fake_get):
            self.assertEqual(flibusta.author_id_and_base("франсуа рабле", "https://flibusta.is"),
                             ("2583", "https://flub.flibusta.is/"))
        self.assertEqual(len(calls), 3)

    def test_exact_author_wins_over_unrelated_page_links(self):
        page = '''<div id="main"><ul>
          <li><a href="/a/10084">Франсуа Рабле</a></li>
          <li><a href="/a/57216">Журавлев</a></li>
          <li><a href="/a/133220">Уленгов</a></li>
        </ul></div>'''.encode()

        with patch.object(flibusta, "get", return_value=(page, {}, "https://flibusta.is/booksearch")):
            self.assertEqual(flibusta.author_id_and_base("франсуа рабле", "https://flibusta.is"),
                             ("10084", "https://flibusta.is/"))

    def test_search_by_synonym_uses_primary_author_url(self):
        page = '''<h3>Найденные писатели (1 - 1 из 1):</h3><ul>
          <li><a href="/a/20391">Джон Соул</a> (через синоним
          <a href="/a/38360"><span>John</span> <span>Saul</span></a>) (5 книг)</li>
        </ul>'''.encode()

        with patch.object(flibusta, "get", return_value=(page, {}, "https://flibusta.is/booksearch")):
            self.assertEqual(flibusta.author_id_and_base("John Saul", "https://flibusta.is"),
                             ("20391", "https://flibusta.is/"))
            self.assertEqual(flibusta.author_id_and_base("Джон Соул", "https://flibusta.is"),
                             ("20391", "https://flibusta.is/"))

    def test_get_retries_503(self):
        calls = []

        class Response:
            headers = {}
            url = "https://example.test/booksearch"

            def __init__(self):
                self.done = False

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, size):
                if self.done:
                    return b""
                self.done = True
                return b"ok"

        def fake_open(request, timeout):
            calls.append(request.full_url)
            if len(calls) < 3:
                raise HTTPError(request.full_url, 503, "Backend fetch failed", {}, None)
            return Response()

        with patch.object(flibusta, "urlopen", fake_open), patch.object(flibusta._cancel_requested, "wait", return_value=False):
            self.assertEqual(flibusta.get("https://example.test/booksearch")[0], b"ok")
        self.assertEqual(len(calls), 3)

    def test_ctrl_c_stops_downloads_without_traceback_and_keeps_pending_log(self):
        self.addCleanup(flibusta._cancel_requested.clear)
        feed = FEED_0.replace(b"/opds/author/1/alphabet/1", b"")

        def fake_get(url):
            if "/opds/" in url:
                return feed, {}, url
            flibusta._cancel_requested.wait(timeout=2)
            raise flibusta.DownloadCancelled()

        def interrupt_futures(_):
            raise KeyboardInterrupt()

        with tempfile.TemporaryDirectory() as folder, patch.object(flibusta, "get", fake_get), patch.object(flibusta, "as_completed", interrupt_futures), patch.object(flibusta.Path, "cwd", return_value=Path(folder)):
            config = Path(folder) / "config.ini"
            config.write_text("[defaults]\nlanguages = ru\nformats = fb2\n[site]\nmirror = https://mirror.test\n")
            output = io.StringIO()
            with redirect_stderr(output), redirect_stdout(io.StringIO()):
                self.assertEqual(flibusta.main(["1", "--config", str(config)]), 130)
            self.assertIn("Прервано пользователем", output.getvalue())
            self.assertNotIn("Traceback", output.getvalue())
            entry = next(iter(json.loads((Path(folder) / flibusta.LOG_NAME).read_text())["entries"].values()))
            self.assertEqual(entry["status"], "pending")

    def test_spinner_uses_plain_status_inside_mc(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        output = Terminal()
        with patch.dict(flibusta.os.environ, {"MC_SID": "123"}), redirect_stderr(output):
            with flibusta.Spinner("Читаю каталог"):
                pass
        self.assertEqual(output.getvalue(), "Читаю каталог\n")

    def test_downloads_run_in_parallel_and_stdout_has_only_filenames(self):
        feed = FEED_0.replace(b"/opds/author/1/alphabet/1", b"")
        barrier = threading.Barrier(2)

        def fake_get(url):
            if "/opds/" in url:
                return feed, {}, url
            barrier.wait(timeout=2)
            return (archive() if url.endswith("/fb2") else b"%PDF-1.4"), {}, url

        with tempfile.TemporaryDirectory() as folder, patch.object(flibusta, "get", fake_get), patch.object(flibusta.Path, "cwd", return_value=Path(folder)):
            config = Path(folder) / "config.ini"
            config.write_text("[defaults]\nlanguages = ru\nformats = fb2\n[site]\nmirror = https://mirror.test\n[downloads]\nworkers = 2\n")
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                self.assertEqual(flibusta.main(["1", "--config", str(config), "-f", "fb2", "pdf"]), 0)
            self.assertEqual(set(output.getvalue().splitlines()),
                             {"Russian Book [10].fb2.zip", "Russian Book [10].pdf"})
            entries = json.loads((Path(folder) / flibusta.LOG_NAME).read_text())["entries"]
            self.assertEqual({entry["status"] for entry in entries.values()}, {"downloaded"})

    def test_sync_uses_nested_files_without_json_log(self):
        feed = '''<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><id>/b/10</id><title>Уже есть</title><content>Язык: ru</content>
            <link href="/b/10/fb2" type="application/fb2+zip" /></entry>
          <entry><id>/b/20</id><title>Без ID</title><content>Язык: ru</content>
            <link href="/b/20/pdf" type="application/pdf" /></entry>
          <entry><id>/b/30</id><title>Новая книга</title><content>Язык: ru</content>
            <link href="/b/30/fb2" type="application/fb2+zip" /></entry>
        </feed>'''.encode()
        search = '<a href="/a/2583">Альфонс Доде</a>'.encode()
        calls = []

        def fake_get(url):
            calls.append(url)
            if "booksearch?" in url:
                return search, {}, url
            if "/opds/author/" in url:
                return feed, {}, url
            return archive(), {}, url

        with tempfile.TemporaryDirectory() as folder, patch.object(flibusta, "get", fake_get), patch.object(flibusta.Path, "cwd", return_value=Path(folder)):
            root = Path(folder)
            (root / "sub" / "deeper").mkdir(parents=True)
            (root / "sub" / "Уже есть [10].fb2").write_text("book")
            (root / "sub" / "deeper" / "Без ID.pdf").write_bytes(b"%PDF-1.4")
            (root / flibusta.LOG_NAME).write_text("broken json")
            config = root / "config.ini"
            config.write_text("[defaults]\nlanguages = ru\nformats = fb2 pdf\n[site]\nmirror = https://mirror.test\n")
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                self.assertEqual(flibusta.main(["-s", "-a", "альфонс доде", "--config", str(config)]), 0)
            self.assertEqual(output.getvalue().splitlines(), ["Новая книга [30].fb2.zip"])
            self.assertEqual([url for url in calls if "/b/" in url], ["https://mirror.test/b/30/fb2"])
            self.assertEqual((root / flibusta.LOG_NAME).read_text(), "broken json")

    def test_sync_recognizes_archives_and_avoids_ambiguous_titles(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "nested").mkdir()
            (root / "nested" / "Archive [11].pdf.rar").write_bytes(b"rar")
            (root / "nested" / "Archive [12].fb2.zip").write_bytes(b"zip")
            (root / "Duplicate.fb2").write_text("book")
            entries = [
                {"book_id": "11", "title": "Archive", "format": "pdf"},
                {"book_id": "12", "title": "Archive", "format": "fb2"},
                {"book_id": "13", "title": "Duplicate", "format": "fb2"},
                {"book_id": "14", "title": "Duplicate", "format": "fb2"},
            ]
            self.assertEqual([entry["book_id"] for entry in flibusta.missing_entries(entries, root)],
                             ["13", "14"])

    def test_sync_and_retry_are_exclusive(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            flibusta.main(["-s", "-r", "-a", "альфонс доде"])

    def test_sync_records_failure_for_retry_without_using_log_to_decide_missing(self):
        feed = FEED_0.replace(b"/opds/author/1/alphabet/1", b"")
        calls = []

        def fake_get(url):
            calls.append(url)
            if "/opds/" in url:
                return feed, {}, url
            if len([item for item in calls if "/b/10/fb2" in item]) == 1:
                raise TimeoutError("timed out")
            return archive(), {}, url

        with tempfile.TemporaryDirectory() as folder, patch.object(flibusta, "get", fake_get), patch.object(flibusta.Path, "cwd", return_value=Path(folder)):
            root = Path(folder)
            config = root / "config.ini"
            config.write_text("[defaults]\nlanguages = ru\nformats = fb2\n[site]\nmirror = https://mirror.test\n")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(flibusta.main(["-s", "1", "--config", str(config)]), 1)
            log_path = root / flibusta.LOG_NAME
            entry = next(iter(json.loads(log_path.read_text())["entries"].values()))
            self.assertEqual(entry["status"], "failed")
            before = len([url for url in calls if "/opds/" in url])
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(flibusta.main(["-r", "--config", str(config)]), 0)
            self.assertEqual(len([url for url in calls if "/opds/" in url]), before)
            self.assertTrue((root / "Russian Book [10].fb2.zip").exists())

    def test_sync_clears_stale_failure_when_book_is_in_subfolder(self):
        feed = FEED_0.replace(b"/opds/author/1/alphabet/1", b"")
        calls = []

        def fake_get(url):
            calls.append(url)
            if "/opds/" in url:
                return feed, {}, url
            raise AssertionError("existing book was downloaded")

        with tempfile.TemporaryDirectory() as folder, patch.object(flibusta, "get", fake_get), patch.object(flibusta.Path, "cwd", return_value=Path(folder)):
            root = Path(folder)
            (root / "nested").mkdir()
            (root / "nested" / "Russian Book [10].fb2").write_text("book")
            config = root / "config.ini"
            config.write_text("[defaults]\nlanguages = ru\nformats = fb2\n[site]\nmirror = https://mirror.test\n")
            log_path = root / flibusta.LOG_NAME
            log_path.write_text(json.dumps({"version": 1, "entries": {
                "https://mirror.test/b/10/fb2|fb2": {
                    "book_id": "10", "title": "Russian Book", "language": "ru",
                    "format": "fb2", "url": "https://mirror.test/b/10/fb2",
                    "mime": "application/fb2+zip", "status": "failed", "error": "timed out",
                }
            }}))
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(flibusta.main(["-s", "1", "--config", str(config)]), 0)
                self.assertEqual(flibusta.main(["-r", "--config", str(config)]), 0)
            entry = next(iter(json.loads(log_path.read_text())["entries"].values()))
            self.assertEqual(entry["status"], "existing")
            self.assertEqual(entry["error"], None)
            self.assertEqual(len([url for url in calls if "/b/" in url]), 0)


if __name__ == "__main__":
    unittest.main()
