"""Tell the first failure of a streak, and its recovery, apart from the repeats in between.

A value read on every status poll fails on every poll for as long as its device is down, so
logging each failure buries the log in one fact (#260). A caller asks `begins` on a failure
and `ends` on a success, and logs only when the answer is True.
"""

from __future__ import annotations

import threading


class FailureStreaks:
    def __init__(self):
        self._failing: set[str] = set()
        self._lock = threading.Lock()

    def begins(self, key: str) -> bool:
        """Record a failure of `key`; True only if `key` was not already failing."""
        with self._lock:
            if key in self._failing:
                return False
            self._failing.add(key)
            return True

    def ends(self, key: str) -> bool:
        """Record a success of `key`; True only if `key` was failing until now."""
        with self._lock:
            if key not in self._failing:
                return False
            self._failing.discard(key)
            return True
