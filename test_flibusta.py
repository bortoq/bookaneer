import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
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

    def test_cli_filters_and_extracts_zip(self):
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
            self.assertEqual((Path(folder) / "Russian Book [10].fb2").read_text(), "book text")
            self.assertNotIn("/b/10/pdf", calls)
            self.assertEqual(flibusta.main(["1", "--config", str(config), "-l", "en", "-f", "pdf"]), 0)
            self.assertEqual((Path(folder) / "English Book [11].pdf.rar").read_bytes(), pages["/b/11/download"])

    def test_author_url(self):
        self.assertEqual(flibusta.author_id_and_base("http://flibusta.is/a/2583", "https://mirror.test"),
                         ("2583", "http://flibusta.is/"))


if __name__ == "__main__":
    unittest.main()
