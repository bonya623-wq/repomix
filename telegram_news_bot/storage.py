"""
Persistent JSON storage for published article UIDs.
Keeps a capped set to prevent unbounded growth (~30 days of articles).
"""
import json
import logging
import os
from collections import deque
from typing import Deque

logger = logging.getLogger(__name__)

MAX_STORED = 10_000  # ~30 days at ~15 articles/day


class PublishedStorage:
    def __init__(self, path: str) -> None:
        self._path = path
        self._uids: Deque[str] = deque(maxlen=MAX_STORED)
        self._uid_set: set[str] = set()
        self._load()

    # ------------------------------------------------------------------
    def is_published(self, uid: str) -> bool:
        return uid in self._uid_set

    def mark_published(self, uid: str) -> None:
        if uid in self._uid_set:
            return
        if len(self._uids) == MAX_STORED:
            # Remove the oldest entry from the fast-lookup set
            oldest = self._uids[0]
            self._uid_set.discard(oldest)
        self._uids.append(uid)
        self._uid_set.add(uid)
        self._save()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as fh:
                data: list[str] = json.load(fh)
            for uid in data[-MAX_STORED:]:
                self._uids.append(uid)
                self._uid_set.add(uid)
            logger.info("Loaded %d published UIDs from %s", len(self._uids), self._path)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load storage (%s), starting fresh.", exc)

    def _save(self) -> None:
        try:
            with open(self._path, "w", encoding="utf-8") as fh:
                json.dump(list(self._uids), fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.error("Failed to save storage: %s", exc)
