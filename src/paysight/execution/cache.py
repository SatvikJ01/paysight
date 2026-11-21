"""In-memory LRU cache of query results, keyed by validated SQL and the data's content hash."""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict

import pandas as pd


def cache_key(sql: str, content_hash: str) -> str:
    """``sha256(normalized_sql + content_hash)``; whitespace differences do not matter."""
    normalized = " ".join(sql.split())
    return hashlib.sha256((normalized + content_hash).encode()).hexdigest()


class QueryCache:
    """A small, thread-safe LRU cache. Stored frames are copied in and out."""

    def __init__(self, max_entries: int) -> None:
        self._max = max_entries
        self._data: OrderedDict[str, pd.DataFrame] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> pd.DataFrame | None:
        """Return a copy of the cached frame, or ``None``."""
        with self._lock:
            frame = self._data.get(key)
            if frame is None:
                return None
            self._data.move_to_end(key)
            return frame.copy()

    def put(self, key: str, frame: pd.DataFrame) -> None:
        """Store a copy of ``frame``, evicting the least recently used entry if full."""
        with self._lock:
            self._data[key] = frame.copy()
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
