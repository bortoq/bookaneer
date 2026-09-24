"""HTTP requests and bounded, cancellable streaming downloads."""

import io
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

USER_AGENT = "flibusta-author-downloader/2.0"
CHUNK_SIZE = 64 * 1024
CATALOG_LIMIT = 8 * 1024 * 1024
MAX_ATTEMPTS = 3
TRANSIENT_STATUSES = {429, 500, 502, 503, 504}


def _retry_delay(error, attempt):
    if isinstance(error, requests.HTTPError):
        response = error.response
        if response is None or response.status_code not in TRANSIENT_STATUSES:
            return None
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                try:
                    delay = (parsedate_to_datetime(retry_after) -
                             datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    delay = None
            if delay is not None:
                return min(10, max(0, delay))
    elif not isinstance(error, (requests.Timeout, requests.ConnectionError,
                                requests.exceptions.ChunkedEncodingError)) or isinstance(
                                    error, requests.exceptions.SSLError):
        return None
    return attempt + 1


class DownloadCancelled(Exception):
    pass


class Transport:
    def __init__(self, cancel):
        self.cancel = cancel
        self.local = threading.local()

    def _session(self):
        if not hasattr(self.local, "session"):
            session = requests.Session()
            session.headers["User-Agent"] = USER_AGENT
            self.local.session = session
        return self.local.session

    def fetch(self, url, output, limit=None):
        """Write a response to a seekable file; restart from zero on read errors."""
        for attempt in range(MAX_ATTEMPTS):
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
            except requests.RequestException as exc:
                delay = _retry_delay(exc, attempt)
                if delay is None or attempt == MAX_ATTEMPTS - 1:
                    raise
                if self.cancel.wait(delay):
                    raise DownloadCancelled()

    def get_bytes(self, url):
        output = io.BytesIO()
        headers, actual_url = self.fetch(url, output, CATALOG_LIMIT)
        return output.getvalue(), headers, actual_url
