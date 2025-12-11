import logging
from collections import deque
from contextlib import contextmanager
from time import perf_counter
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_recent_timings: deque[Dict[str, Any]] = deque(maxlen=20)


def record_timing(label: str, duration_ms: float) -> None:
    _recent_timings.append({"label": label, "duration_ms": round(duration_ms, 2)})


def get_recent_timings() -> List[Dict[str, Any]]:
    return list(_recent_timings)


@contextmanager
def time_block(label: str):
    start = perf_counter()
    try:
        yield
    finally:
        duration_ms = (perf_counter() - start) * 1000.0
        record_timing(label, duration_ms)
        logger.debug(f"[perf] {label} took {duration_ms:.2f}ms")
