from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, Optional

from services.activity_log import add_activity

_LOCK = Lock()
_STATE: Dict[str, Dict[str, Any]] = {}
_LAST_LOG_SIGNATURE: Dict[str, tuple] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def update_worker_runtime_state(worker_name: str, **updates: Any) -> Dict[str, Any]:
    if not worker_name:
        worker_name = "unknown"
    with _LOCK:
        state = _STATE.setdefault(
            worker_name,
            {
                "worker_name": worker_name,
                "runtime_status": "idle",
                "last_loop_started_utc": None,
                "last_loop_finished_utc": None,
                "next_wake_utc": None,
                "last_message": None,
                "current_task": None,
                "queue_count": None,
                "active_window": None,
                "updated_at_utc": None,
            },
        )
        for key, value in updates.items():
            if key in state:
                state[key] = value
        state["updated_at_utc"] = _now_iso()
        return deepcopy(state)


def record_worker_runtime_state(
    worker_name: str,
    *,
    runtime_status: str,
    last_message: str,
    activity_level: Optional[str] = None,
    **updates: Any,
) -> Dict[str, Any]:
    state = update_worker_runtime_state(
        worker_name,
        runtime_status=runtime_status,
        last_message=last_message,
        **updates,
    )
    if activity_level:
        signature = (
            state.get("runtime_status"),
            state.get("last_message"),
            state.get("current_task"),
            state.get("active_window"),
        )
        with _LOCK:
            previous = _LAST_LOG_SIGNATURE.get(worker_name)
            if previous != signature:
                _LAST_LOG_SIGNATURE[worker_name] = signature
                add_activity(activity_level, f"{worker_name}: {last_message}")
    return state


def get_worker_runtime_state(worker_name: str) -> Dict[str, Any]:
    with _LOCK:
        return deepcopy(_STATE.get(worker_name, {"worker_name": worker_name}))
