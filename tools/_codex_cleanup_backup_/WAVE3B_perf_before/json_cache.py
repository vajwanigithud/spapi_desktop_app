import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VENDOR_POS_CACHE = ROOT / "vendor_pos_cache.json"
DEFAULT_ASIN_CACHE_PATH = ROOT / "asin_image_cache.json"
DEFAULT_PO_TRACKER_PATH = ROOT / "po_tracker.json"
DEFAULT_OOS_STATE_PATH = ROOT / "oos_state.json"


def _read_json(path: Path, default: Any, raise_on_error: bool = False) -> Any:
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except Exception as exc:
        logger.warning(f"[json_cache] Failed to read {path}: {exc}")
        if raise_on_error:
            raise
        return default


def _write_json(path: Path, payload: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning(f"[json_cache] Failed to write {path}: {exc}")


def load_vendor_pos_cache(path: Optional[Path] = None, *, raise_on_error: bool = False) -> Any:
    return _read_json(path or DEFAULT_VENDOR_POS_CACHE, {}, raise_on_error=raise_on_error)


def save_vendor_pos_cache(payload: Any, path: Optional[Path] = None) -> None:
    _write_json(path or DEFAULT_VENDOR_POS_CACHE, payload)


def load_asin_cache(path: Optional[Path] = None) -> Dict[str, Any]:
    cache = _read_json(path or DEFAULT_ASIN_CACHE_PATH, {})
    if not isinstance(cache, dict):
        return {}
    return {k: v for k, v in cache.items() if isinstance(v, dict) and (v.get("title") or v.get("image"))}


def save_asin_cache(cache: Dict[str, Any], path: Optional[Path] = None) -> None:
    _write_json(path or DEFAULT_ASIN_CACHE_PATH, cache)


def load_po_tracker(path: Optional[Path] = None) -> Dict[str, Any]:
    data = _read_json(path or DEFAULT_PO_TRACKER_PATH, {})
    return data if isinstance(data, dict) else {}


def save_po_tracker(tracker: Dict[str, Any], path: Optional[Path] = None) -> None:
    _write_json(path or DEFAULT_PO_TRACKER_PATH, tracker)


def load_oos_state(path: Optional[Path] = None) -> Dict[str, Any]:
    data = _read_json(path or DEFAULT_OOS_STATE_PATH, {})
    return data if isinstance(data, dict) else {}


def save_oos_state(state: Dict[str, Any], path: Optional[Path] = None) -> None:
    _write_json(path or DEFAULT_OOS_STATE_PATH, state)
