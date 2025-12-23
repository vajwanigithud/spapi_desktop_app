import threading

_logged_keys = set()
_lock = threading.Lock()


def log_once(logger, key: str, level: str, message: str) -> None:
    """
    Log a message exactly once per process for the given key.
    """
    with _lock:
        if key in _logged_keys:
            return
        _logged_keys.add(key)

    getattr(logger, level)(message)
