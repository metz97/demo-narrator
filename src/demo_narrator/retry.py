"""Retry with exponential backoff for network calls."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryableError(Exception):
    """Wrap an error that should be retried (e.g. HTTP 429/5xx)."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def with_backoff(
    fn: Callable[[], T],
    *,
    description: str,
    max_attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn`` until it succeeds or ``max_attempts`` is exhausted.

    ``fn`` signals a retryable failure by raising RetryableError; any other
    exception propagates immediately.
    """
    last_exc: RetryableError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except RetryableError as exc:
            last_exc = exc
            if attempt == max_attempts:
                break
            delay = min(base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5), max_delay)
            if exc.retry_after is not None:
                delay = max(delay, exc.retry_after)
            logger.warning(
                "%s failed (attempt %d/%d): %s -- retrying in %.1fs",
                description,
                attempt,
                max_attempts,
                exc,
                delay,
            )
            sleep(delay)
    raise last_exc if last_exc is not None else RuntimeError(f"{description}: no attempts made")
