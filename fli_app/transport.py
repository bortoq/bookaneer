"""HTTP requests and bounded, cancellable streaming downloads."""

import io
import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

USER_AGENT = "flibusta-author-downloader/2.0"
CHUNK_SIZE = 64 * 1024
CATALOG_LIMIT = 8 * 1024 * 1024


class DownloadCancelled(Exception):
    pass


class Transport:
    def __init__(self, cancel):
        self.cancel = cancel
        self.local = threading.local()

    def _session(self):
        if not hasattr(self.local, "session"):
            session = requests.Session()
            retry = Retry(total=2, backoff_factor=0.5,
                          status_forcelist=(429, 500, 502, 503, 504),
                          allowed_methods=("GET",), respect_retry_after_header=True,
                          retry_after_max=10)
            adapter = HTTPAdapter(max_retries=retry)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            session.headers["User-Agent"] = USER_AGENT
            self.local.session = session
        return self.local.session

    def fetch(self, url, output, limit=None):
        """Write a response to a seekable file; restart from zero on read errors."""
        for attempt in range(3):
            if self.cancel.is_set():
                raise DownloadCancelled()
            output.seek(0)
            output.truncate()
            try:
                with self._session().get(url, stream=True, timeout=(10, 30)) as response:
                    response.raise_for_status()
                    size = 0
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        if self.cancel.is_set():
                            raise DownloadCancelled()
                        size += len(chunk)
                        if limit is not None and size > limit:
                            raise ValueError("ответ сервера превышает допустимый размер")
                        output.write(chunk)
                    return dict(response.headers), response.url
            except requests.RequestException:
                if attempt == 2:
                    raise
                if self.cancel.wait(attempt + 1):
                    raise DownloadCancelled()

    def get_bytes(self, url):
        output = io.BytesIO()
        headers, actual_url = self.fetch(url, output, CATALOG_LIMIT)
        return output.getvalue(), headers, actual_url
