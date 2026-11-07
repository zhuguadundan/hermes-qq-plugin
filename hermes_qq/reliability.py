"""Small durable state and bounded download primitives for the QQ adapter."""
from __future__ import annotations

import math
from pathlib import Path
import sqlite3
import threading
import time

import requests


def number(extra: dict, name: str, default: float, minimum: float = 0) -> float:
    value = extra.get(name, default)
    if value is None:
        value = default
    try:
        value = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"QQ {name} must be a number") from exc
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"QQ {name} must be at least {minimum}")
    return value


class EventJournal:
    """Record successfully accepted events, not transient processing attempts.

    A cold installation cannot know which older messages already ran. Its
    creation time is the lower bound for recovering unknown history requests.
    """
    RETENTION = 7 * 86400

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (key TEXT PRIMARY KEY, ts REAL NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value REAL NOT NULL)')
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('created_at', ?)", (time.time(),))
        self.db.commit()
        self.created_at = self.db.execute("SELECT value FROM metadata WHERE key='created_at'").fetchone()[0]
        self.prune()

    def contains(self, key: str) -> bool:
        return self.db.execute('SELECT 1 FROM events WHERE key=?', (key,)).fetchone() is not None

    def finish(self, keys) -> None:
        now = time.time()
        self.db.executemany('INSERT OR REPLACE INTO events VALUES (?, ?)', [(key, now) for key in keys])
        self.db.commit()

    def prune(self) -> None:
        self.db.execute('DELETE FROM events WHERE ts < ?', (time.time() - self.RETENTION,))
        self.db.commit()

    def close(self) -> None:
        self.db.close()


def stream_download(url: str, dest: Path, max_bytes: int, timeout: float,
                    stop: threading.Event, total_timeout: float = 600) -> int:
    """Stream with a size bound, deadline, and cooperative shutdown.

    The caller owns a unique destination. A failed request never exposes a
    partial final file. Existing .part files are never overwritten.
    """
    part = dest.with_name(dest.name + '.part')
    total = 0
    owns_part = False
    started = time.monotonic()
    def check():
        if stop.is_set():
            raise InterruptedError('QQ download stopped')
        if time.monotonic() - started > total_timeout:
            raise TimeoutError('QQ download total deadline exceeded')
    try:
        check()
        with requests.get(url, timeout=(min(timeout, 10), min(timeout, 5)), stream=True) as response:
            response.raise_for_status()
            declared = response.headers.get('Content-Length')
            if declared and max_bytes and int(declared) > max_bytes:
                raise ValueError('file exceeds QQ single-file limit')
            with part.open('xb') as handle:
                owns_part = True
                for chunk in response.iter_content(65536):
                    check()
                    if not chunk:
                        continue
                    total += len(chunk)
                    if max_bytes and total > max_bytes:
                        raise ValueError('file exceeds QQ single-file limit')
                    handle.write(chunk)
            check()
            if declared and total != int(declared):
                raise ValueError('incomplete QQ file download')
        part.replace(dest)
    except BaseException:
        if owns_part:
            part.unlink(missing_ok=True)
        raise
    return total
