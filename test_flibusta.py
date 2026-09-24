import io
from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from fli_app import catalog, cli, files
from fli_app.journal import Journal, LOG_NAME
from fli_app.transport import Transport


FEED = '''<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><id>/b/10</id><title>Russian Book</title><content>Язык: ru</content>
    <link rel="http://opds-spec.org/image/thumbnail" href="/i/10" type="image/jpeg" />
    <link rel="http://opds-spec.org/acquisition" href="/b/10/fb2" type="application/fb2+zip" />
    <link rel="http://opds-spec.org/acquisition" href="/b/10/pdf" type="application/pdf" /></entry>
  <entry><id>/b/11</id><title>English Book</title><content>Язык: en</content>
    <link rel="http://opds-spec.org/acquisition" href="/b/11/pdf" type="application/pdf" /></entry>
</feed>'''.encode()
SEARCH = '''<h3>Найденные писатели (1 - 1 из 1):</h3><ul>
  <li><a href="/a/20391">Джон Соул</a> (через синоним
  <a href="/a/38360"><span>John</span> <span>Saul</span></a>)</li>
</ul>'''.encode()


def fb2_zip(content=b'<FictionBook><body/></FictionBook>'):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('book.fb2', content)
    return output.getvalue()


def document_zip(fmt):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        if fmt == 'docx':
            archive.writestr('[Content_Types].xml',
                             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
            archive.writestr('_rels/.rels',
                             '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')
            archive.writestr('word/document.xml',
                             '<document xmlns="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
        elif fmt == 'epub':
            archive.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
            archive.writestr('META-INF/container.xml',
                             '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                             '<rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>')
            archive.writestr('OPS/book.opf', '<package xmlns="http://www.idpf.org/2007/opf"/>')
    return output.getvalue()


class FixtureHandler(BaseHTTPRequestHandler):
    responses = {}
    calls = []
    lock = threading.Lock()

    def do_GET(self):
        with self.lock:
            self.calls.append(self.path)
            item = self.responses.get(self.path, (404, b'missing', 'text/plain'))
            if isinstance(item, list):
                status, body, content_type = item.pop(0) if len(item) > 1 else item[0]
            else:
                status, body, content_type = item
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.output = Path(self.folder.name)
        self.config = self.output / 'flibusta.ini'
        self.config.write_text('[defaults]\nlanguages = ru\nformats = fb2\n'
                               '[site]\nmirror = http://127.0.0.1\n'
                               '[downloads]\nworkers = 2\nextract_zip = no\n')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.config.write_text(self.config.read_text().replace('http://127.0.0.1\n', self.base + '\n'))
        FixtureHandler.calls = []
        FixtureHandler.responses = {
            '/opds/author/1/alphabet/0': (200, FEED, 'application/atom+xml'),
            '/b/10/fb2': (200, fb2_zip(), 'application/fb2+zip'),
            '/b/10/pdf': (200, b'%PDF-1.4\nbook', 'application/pdf'),
            '/b/11/pdf': (200, b'%PDF-1.4\nbook', 'application/pdf'),
        }
        self.cwd_patch = patch.object(cli.Path, 'cwd', return_value=self.output)
        self.cwd_patch.start()
        self.addCleanup(self.cwd_patch.stop)

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main([*args, '--config', str(self.config)])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_downloads_requested_formats_and_records_sqlite_journal(self):
        code, stdout, stderr = self.run_cli('1', '-f', 'fb2', 'pdf')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(set(stdout.splitlines()), {'Russian Book [10].fb2.zip',
                                                  'Russian Book [10].pdf'})
        self.assertTrue((self.output / 'Russian Book [10].fb2.zip').exists())
        with sqlite3.connect(self.output / LOG_NAME) as connection:
            rows = connection.execute('SELECT status, format FROM downloads').fetchall()
        self.assertEqual(set(rows), {('downloaded', 'fb2'), ('downloaded', 'pdf')})

    def test_language_filter_and_ini_multiple_formats(self):
        self.config.write_text(self.config.read_text().replace('formats = fb2',
                                                               'formats = fb2 pdf'))
        code, stdout, stderr = self.run_cli('1', '-l', 'en')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout.splitlines(), ['English Book [11].pdf'])

    def test_bare_language_and_format_flags_select_all_catalog_variants(self):
        code, stdout, stderr = self.run_cli('1', '-l', '-f')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(set(stdout.splitlines()), {'Russian Book [10].fb2.zip',
                                                  'Russian Book [10].pdf',
                                                  'English Book [11].pdf'})

    def test_additional_catalog_formats_and_languages(self):
        feed = '''<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><id>/b/12</id><title>Document</title>
            <content>Формат: docx Язык: de</content>
            <link rel="http://opds-spec.org/acquisition" href="/b/12/download"
                  type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"/></entry>
          <entry><id>/b/13</id><title>Kindle</title>
            <content>Формат: azw3 Язык: ru~ru-petr1708</content>
            <link rel="http://opds-spec.org/acquisition" href="/b/13/download"
                  type="application/octet-stream"/></entry>
          <entry><id>/b/14</id><title>Scans</title>
            <content>Формат: djvu Язык: uk</content>
            <link rel="http://opds-spec.org/acquisition" href="/b/14/download"
                  type="application/djvu+zip"/></entry>
        </feed>'''.encode()
        FixtureHandler.responses['/opds/author/1/alphabet/0'] = (200, feed, 'application/atom+xml')
        FixtureHandler.responses['/b/12/download'] = (200, document_zip('docx'), 'application/octet-stream')
        FixtureHandler.responses['/b/13/download'] = (200, b'\0' * 60 + b'BOOKMOBI' + b'book', 'application/octet-stream')
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr('book.djv', b'AT&TFORMdjvu')
        FixtureHandler.responses['/b/14/download'] = (200, archive.getvalue(), 'application/djvu+zip')

        code, stdout, stderr = self.run_cli('1', '-l', '-f')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(set(stdout.splitlines()), {'Document [12].docx',
                                                  'Kindle [13].azw3',
                                                  'Scans [14].djvu.zip'})
        self.assertEqual((self.output / 'Document [12].docx').read_bytes(),
                         FixtureHandler.responses['/b/12/download'][1])
        code, stdout, stderr = self.run_cli('1', '-l', 'de', '-f', 'docx')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, '')  # Existing file is not overwritten.

    def test_ru_includes_historical_russian_variant(self):
        feed = '''<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><id>/b/13</id><title>Old Russian</title>
            <content>Язык: ru~ru-petr1708</content>
            <link rel="http://opds-spec.org/acquisition" href="/b/13/fb2"
                  type="application/fb2+zip"/></entry>
        </feed>'''.encode()
        FixtureHandler.responses['/opds/author/1/alphabet/0'] = (200, feed, 'application/atom+xml')
        FixtureHandler.responses['/b/13/fb2'] = (200, fb2_zip(), 'application/fb2+zip')
        self.assertEqual(self.run_cli('1')[1].splitlines(), ['Old Russian [13].fb2.zip'])

    def test_wrong_document_archives_and_plain_text_azw3_are_rejected(self):
        feed = '''<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><id>/b/12</id><title>Document</title><content>Формат: docx Язык: ru</content>
            <link rel="http://opds-spec.org/acquisition" href="/b/12/download" type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"/></entry>
          <entry><id>/b/13</id><title>Kindle</title><content>Формат: azw3 Язык: ru</content>
            <link rel="http://opds-spec.org/acquisition" href="/b/13/download" type="application/octet-stream"/></entry>
          <entry><id>/b/14</id><title>EPUB</title><content>Формат: epub Язык: ru</content>
            <link rel="http://opds-spec.org/acquisition" href="/b/14/download" type="application/epub+zip"/></entry>
        </feed>'''.encode()
        FixtureHandler.responses['/opds/author/1/alphabet/0'] = (200, feed, 'application/atom+xml')
        FixtureHandler.responses['/b/12/download'] = (200, fb2_zip(), 'application/octet-stream')
        FixtureHandler.responses['/b/13/download'] = (200, b'kindle content', 'application/octet-stream')
        FixtureHandler.responses['/b/14/download'] = (200, fb2_zip(), 'application/epub+zip')
        code, stdout, stderr = self.run_cli('1', '-f')
        self.assertEqual(code, 1)
        self.assertEqual(stdout, '')
        self.assertEqual(stderr.count('Ошибка:'), 3)
        self.assertFalse(list(self.output.glob('*.docx')))
        self.assertFalse(list(self.output.glob('*.azw3')))
        self.assertFalse(list(self.output.glob('*.epub')))

    def test_valid_epub_package_is_accepted(self):
        feed = '''<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><id>/b/14</id><title>EPUB</title><content>Формат: epub Язык: ru</content>
            <link rel="http://opds-spec.org/acquisition" href="/b/14/download" type="application/epub+zip"/></entry>
        </feed>'''.encode()
        FixtureHandler.responses['/opds/author/1/alphabet/0'] = (200, feed, 'application/atom+xml')
        FixtureHandler.responses['/b/14/download'] = (200, document_zip('epub'), 'application/epub+zip')
        code, stdout, stderr = self.run_cli('1', '-f', 'epub')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout.splitlines(), ['EPUB [14].epub'])

    def test_sync_recognizes_new_formats_and_multi_dot_suffixes(self):
        (self.output / 'nested').mkdir()
        (self.output / 'nested' / 'Document [12].docx').write_bytes(b'docx')
        (self.output / 'nested' / 'Unusual [13].fb.z.zip').write_bytes(b'zip')
        jobs = [{'book_id': '12', 'title': 'Document', 'format': 'docx'},
                {'book_id': '13', 'title': 'Unusual', 'format': 'fb.z'}]
        self.assertEqual(files.missing_entries(jobs, self.output), [])

    def test_original_archive_formats_and_multi_dot_format(self):
        feed = '''<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><id>/b/15</id><title>Comic</title><content>Формат: cbr Язык: en</content>
            <link rel="http://opds-spec.org/acquisition" href="/b/15/download"
                  type="application/octet-stream" /></entry>
          <entry><id>/b/16</id><title>Unusual</title><content>Формат: fb.z Язык: en</content>
            <link rel="http://opds-spec.org/acquisition" href="/b/16/download"
                  type="application/octet-stream" /></entry>
        </feed>'''.encode()
        FixtureHandler.responses['/opds/author/1/alphabet/0'] = (200, feed, 'application/atom+xml')
        FixtureHandler.responses['/b/15/download'] = (200, b'Rar!\x1a\x07data', 'application/octet-stream')
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('book.fb.z', b'book')
        FixtureHandler.responses['/b/16/download'] = (200, output.getvalue(), 'application/octet-stream')
        code, stdout, stderr = self.run_cli('1', '-l', '-f')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(set(stdout.splitlines()), {'Comic [15].cbr', 'Unusual [16].fb.z.zip'})

    def test_retry_rejects_bare_filters(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(['-r', '-l'])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(['-r', '-f'])

    def test_extract_zip_and_sync_recognizes_nested_file(self):
        (self.output / 'nested').mkdir()
        (self.output / 'nested' / 'Russian Book [10].fb2').write_text('already here')
        code, stdout, stderr = self.run_cli('-s', '1')
        self.assertEqual((code, stdout), (0, ''))
        self.assertNotIn('/b/10/fb2', FixtureHandler.calls)
        code, stdout, stderr = self.run_cli('1', '-x')
        self.assertEqual(code, 0, stderr)
        self.assertEqual((self.output / 'Russian Book [10].fb2').read_bytes(),
                         b'<FictionBook><body/></FictionBook>')

    def test_search_synonym_resolves_primary_author(self):
        FixtureHandler.responses['/booksearch?ask=John+Saul&page=0&cha=on'] = (200, SEARCH, 'text/html')
        FixtureHandler.responses['/opds/author/20391/alphabet/0'] = (200, FEED, 'application/atom+xml')
        code, _, stderr = self.run_cli('-a', 'John Saul')
        self.assertEqual(code, 0, stderr)
        self.assertIn('/opds/author/20391/alphabet/0', FixtureHandler.calls)
        self.assertNotIn('/opds/author/38360/alphabet/0', FixtureHandler.calls)

    def test_ambiguous_search_prints_authors_without_creating_journal(self):
        page = '''<h3>Найденные писатели (1 - 2 из 2):</h3><ul>
          <li><a href="/a/2583">Альфонс Доде</a></li>
          <li><a href="/a/99">Жан Доде</a></li></ul>'''.encode()
        FixtureHandler.responses['/booksearch?ask=%D0%B4%D0%BE%D0%B4%D0%B5&page=0&cha=on'] = (
            200, page, 'text/html')
        code, stdout, _ = self.run_cli('-a', 'доде')
        self.assertEqual(code, 1)
        self.assertEqual(stdout.splitlines(), [f'Альфонс Доде: {self.base}/a/2583',
                                               f'Жан Доде: {self.base}/a/99'])
        self.assertFalse((self.output / LOG_NAME).exists())

    def test_retry_failed_download_from_sqlite(self):
        FixtureHandler.responses['/b/10/fb2'] = [(200, b'upstream error', 'text/plain'),
                                                  (200, fb2_zip(), 'application/fb2+zip')]
        code, _, _ = self.run_cli('1')
        self.assertEqual(code, 1)
        with sqlite3.connect(self.output / LOG_NAME) as connection:
            self.assertEqual(connection.execute('SELECT status FROM downloads').fetchone()[0], 'failed')
        before = FixtureHandler.calls.count('/opds/author/1/alphabet/0')
        code, stdout, stderr = self.run_cli('-r')
        self.assertEqual(code, 0, stderr)
        self.assertIn('Russian Book [10].fb2.zip', stdout)
        self.assertEqual(FixtureHandler.calls.count('/opds/author/1/alphabet/0'), before)

    def test_retry_x_extracts_zip(self):
        FixtureHandler.responses['/b/10/fb2'] = [(200, b'bad data', 'text/plain'),
                                                  (200, fb2_zip(), 'application/fb2+zip')]
        self.assertEqual(self.run_cli('1')[0], 1)
        code, stdout, stderr = self.run_cli('-r', '-x')
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout.splitlines(), ['Russian Book [10].fb2'])
        self.assertTrue((self.output / 'Russian Book [10].fb2').exists())

    def test_http_503_is_retried(self):
        FixtureHandler.responses['/b/10/fb2'] = [(503, b'unavailable', 'text/plain'),
                                                  (200, fb2_zip(), 'application/fb2+zip')]
        code, stdout, stderr = self.run_cli('1')
        self.assertEqual(code, 0, stderr)
        self.assertIn('Russian Book [10].fb2.zip', stdout)
        self.assertEqual(FixtureHandler.calls.count('/b/10/fb2'), 2)

    def test_http_retries_are_bounded_and_404_is_permanent(self):
        FixtureHandler.responses['/b/10/fb2'] = (503, b'unavailable', 'text/plain')
        self.assertEqual(self.run_cli('1')[0], 1)
        self.assertEqual(FixtureHandler.calls.count('/b/10/fb2'), 3)
        FixtureHandler.calls = []
        FixtureHandler.responses['/b/10/fb2'] = (404, b'missing', 'text/plain')
        self.assertEqual(self.run_cli('1')[0], 1)
        self.assertEqual(FixtureHandler.calls.count('/b/10/fb2'), 1)

    def test_bad_pdf_response_is_not_published(self):
        FixtureHandler.responses['/b/10/pdf'] = (200, b'backend failure', 'text/plain')
        code, _, _ = self.run_cli('1', '-f', 'pdf')
        self.assertEqual(code, 1)
        self.assertFalse((self.output / 'Russian Book [10].pdf').exists())

    def test_download_size_limit_leaves_no_partial_file(self):
        FixtureHandler.responses['/b/10/pdf'] = (200, b'%PDF-' + b'x' * 200_000, 'application/pdf')
        with patch.object(files, 'BOOK_LIMIT', 100_000):
            code, _, _ = self.run_cli('1', '-f', 'pdf')
        self.assertEqual(code, 1)
        self.assertFalse((self.output / 'Russian Book [10].pdf').exists())
        self.assertFalse(list(self.output.glob('.flibusta-book-*.tmp')))

    def test_oversized_zip_member_is_rejected(self):
        content = b'<FictionBook>' + b'x' * 100 + b'</FictionBook>'
        FixtureHandler.responses['/b/10/fb2'] = (200, fb2_zip(content), 'application/fb2+zip')
        with patch.object(files, 'EXTRACT_LIMIT', 50):
            code, _, _ = self.run_cli('1', '-x')
        self.assertEqual(code, 1)
        self.assertFalse((self.output / 'Russian Book [10].fb2').exists())

    def test_corrupt_sqlite_is_ignored_only_for_sync(self):
        (self.output / LOG_NAME).write_text('not a database')
        code, stdout, _ = self.run_cli('-s', '1')
        self.assertEqual(code, 0)
        self.assertIn('Russian Book [10].fb2.zip', stdout)
        self.assertEqual((self.output / LOG_NAME).read_text(), 'not a database')
        code, _, _ = self.run_cli('-r')
        self.assertEqual(code, 1)

    def test_cancel_during_stream_keeps_pending_job(self):
        with Journal(self.output / LOG_NAME) as journal:
            job = {'url': self.base + '/b/10/fb2', 'format': 'fb2', 'book_id': '10',
                   'title': 'Russian Book', 'language': 'ru', 'mime': 'application/fb2+zip',
                   'extract_zip': 0}
            journal.upsert(job)
            journal.mark_pending(job)
            self.assertEqual(journal.retry_jobs()[0]['status'], 'pending')

    def test_ctrl_c_records_completed_job_and_leaves_other_pending(self):
        jobs = [{'url': self.base + f'/b/{ident}/pdf', 'format': 'pdf', 'book_id': str(ident),
                 'title': f'Book {ident}', 'language': 'ru', 'mime': 'application/pdf',
                 'extract_zip': 0} for ident in (1, 2)]
        with Journal(self.output / LOG_NAME) as journal:
            for job in jobs:
                journal.upsert(job)
            finished = threading.Event()

            def fake_download(job, output, transport, extract):
                if job['book_id'] == '1':
                    finished.set()
                    return True, output / 'Book 1 [1].pdf'
                finished.wait(2)
                transport.cancel.wait(2)
                from fli_app.transport import DownloadCancelled
                raise DownloadCancelled()

            def interrupt(futures):
                finished.wait(2)
                raise KeyboardInterrupt()

            transport = Transport(threading.Event())
            with patch.object(cli, 'as_completed', interrupt), patch.object(cli, 'download_book', fake_download):
                with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                    with self.assertRaises(KeyboardInterrupt):
                        cli.run_downloads(jobs, journal, self.output, 2, transport, 'Test')
            statuses = dict(journal.connection.execute('SELECT book_id, status FROM downloads').fetchall())
            self.assertEqual(statuses, {'1': 'downloaded', '2': 'pending'})

    def test_workers_start_separate_books_concurrently(self):
        barrier = threading.Barrier(2)
        jobs = [{'url': self.base + f'/b/{ident}/pdf', 'format': 'pdf', 'book_id': str(ident),
                 'title': f'Book {ident}', 'language': 'ru', 'mime': 'application/pdf',
                 'extract_zip': 0} for ident in (1, 2)]

        def fake_download(job, output, transport, extract):
            barrier.wait(timeout=2)
            return True, output / f"Book {job['book_id']}.pdf"

        transport = Transport(threading.Event())
        with patch.object(cli, 'download_book', fake_download):
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.run_downloads(jobs, None, self.output, 2, transport, 'Test'), 0)

    def test_cancel_before_submit_preserves_all_retry_jobs(self):
        jobs = [{'url': self.base + f'/b/{ident}/pdf', 'format': 'pdf', 'book_id': str(ident),
                 'title': f'Book {ident}', 'language': 'ru', 'mime': 'application/pdf',
                 'extract_zip': 0} for ident in (1, 2)]
        with Journal(self.output / LOG_NAME) as journal:
            for job in jobs:
                journal.upsert(job)
            transport = Transport(threading.Event())
            with patch.object(cli.ThreadPoolExecutor, 'submit', side_effect=KeyboardInterrupt):
                with redirect_stderr(io.StringIO()), self.assertRaises(KeyboardInterrupt):
                    cli.run_downloads(jobs, journal, self.output, 2, transport, 'Test')
            self.assertEqual(len(journal.retry_jobs()), 2)

    def test_catalog_and_inventory(self):
        books, next_url = catalog.parse_feed(FEED)
        self.assertEqual((len(books), next_url), (2, None))
        (self.output / 'sub').mkdir()
        (self.output / 'sub' / 'Russian Book [10].fb2.zip').write_bytes(b'x')
        jobs = [{'book_id': '10', 'title': 'Russian Book', 'format': 'fb2'},
                {'book_id': '11', 'title': 'English Book', 'format': 'pdf'}]
        self.assertEqual([job['book_id'] for job in files.missing_entries(jobs, self.output)], ['11'])


if __name__ == '__main__':
    unittest.main()
