"""Throughput measurement for the export phase."""

from __future__ import annotations

import time
from collections import deque
from typing import Callable


class RateMeter:
    """Bytes per second over a sliding window (default: the last minute).

    ``add(n)`` records ``n`` more bytes; ``rate()`` is the volume received inside the window divided by
    the window span actually covered, so the figure is meaningful from the first seconds on instead of
    being diluted by an empty minute.
    """

    def __init__(self, window_s: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.window_s = window_s
        self._clock = clock
        self._samples: deque[tuple[float, int]] = deque()  # (timestamp, bytes)
        self._start = clock()

    def add(self, n: int) -> None:
        now = self._clock()
        self._samples.append((now, n))
        self._trim(now)

    def rate(self) -> float:
        now = self._clock()
        self._trim(now)
        if not self._samples:
            return 0.0
        # divide by the window actually covered so far (elapsed time until the meter is a minute old);
        # a stall inside the window lowers the figure instead of being skipped
        span = min(self.window_s, now - self._start)
        total = sum(n for _, n in self._samples)
        return total / max(span, 1e-3)

    def _trim(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._samples and self._samples[0][0] <= cutoff:
            self._samples.popleft()
