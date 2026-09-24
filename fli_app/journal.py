"""Transactional download journal."""

import sqlite3

LOG_NAME = "flibusta-downloads.sqlite3"


class Journal:
    def __init__(self, path):
        self.connection = sqlite3.connect(path)
        try:
            self.connection.row_factory = sqlite3.Row
            with self.connection:
                self.connection.execute("""CREATE TABLE IF NOT EXISTS downloads (
                url TEXT NOT NULL,
                format TEXT NOT NULL,
                book_id TEXT NOT NULL,
                title TEXT NOT NULL,
                language TEXT NOT NULL,
                mime TEXT NOT NULL,
                extract_zip INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                file TEXT,
                PRIMARY KEY (url, format)
                )""")
                if self.connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("повреждён журнал загрузок")
        except Exception:
            self.connection.close()
            raise

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def upsert(self, job):
        with self.connection:
            self.connection.execute("""INSERT INTO downloads
                (url, format, book_id, title, language, mime, extract_zip)
                VALUES (:url, :format, :book_id, :title, :language, :mime, :extract_zip)
                ON CONFLICT(url, format) DO UPDATE SET
                    book_id=excluded.book_id, title=excluded.title,
                    language=excluded.language, mime=excluded.mime,
                    extract_zip=excluded.extract_zip""", job)

    def retry_jobs(self):
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM downloads WHERE status IN ('failed', 'pending')")]

    def status(self, job):
        row = self.connection.execute("SELECT status FROM downloads WHERE url=? AND format=?",
                                      (job["url"], job["format"])).fetchone()
        return row[0] if row else None

    def mark_pending(self, job):
        with self.connection:
            self.connection.execute("""UPDATE downloads SET status='pending',
                attempts=attempts+1, error=NULL WHERE url=? AND format=?""",
                (job["url"], job["format"]))

    def queue(self, jobs):
        with self.connection:
            self.connection.executemany("""UPDATE downloads SET status='pending', error=NULL
                WHERE url=:url AND format=:format""", jobs)

    def finish(self, job, status, file=None, error=None):
        with self.connection:
            self.connection.execute("""UPDATE downloads SET status=?, file=?, error=?
                WHERE url=? AND format=?""",
                (status, file, error, job["url"], job["format"]))
