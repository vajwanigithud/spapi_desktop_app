from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

try:
    from dotenv import dotenv_values, load_dotenv
except ImportError:  # pragma: no cover
    dotenv_values = None
    load_dotenv = None


MANAGED_ENV_KEYS = [
    "LWA_CLIENT_ID",
    "LWA_CLIENT_SECRET",
    "LWA_REFRESH_TOKEN",
    "MARKETPLACE_IDS",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ROLE_ARN",
    "AWS_REGION",
    "SPAPI_REGION",
    "DF_REMITTANCE_IMAP_USER",
    "DF_REMITTANCE_IMAP_PASS",
    "DF_REMITTANCE_GMAIL_LABEL",
    "DF_REMITTANCE_IMAP_MAILBOX",
    "DF_REMITTANCE_MAX_MESSAGES",
]

SECRET_KEYS = {
    "LWA_CLIENT_SECRET",
    "LWA_REFRESH_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "DF_REMITTANCE_IMAP_PASS",
}


@dataclass(frozen=True)
class EnvSaveResult:
    env_path: Path
    backup_path: Path


def default_env_path() -> Path:
    return Path(__file__).resolve().parent.parent / ".env"


def read_env_values(env_path: Optional[Path] = None) -> Dict[str, str]:
    path = env_path or default_env_path()
    if dotenv_values and path.exists():
        parsed = dotenv_values(path)
        return {key: "" if value is None else str(value) for key, value in parsed.items()}

    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_assignment(line)
        if parsed:
            key, value = parsed
            values[key] = value
    return values


def read_managed_values(env_path: Optional[Path] = None) -> Dict[str, str]:
    values = read_env_values(env_path)
    return {key: values.get(key, "") for key in MANAGED_ENV_KEYS}


def save_managed_values(values: Dict[str, object], env_path: Optional[Path] = None) -> EnvSaveResult:
    path = env_path or default_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    updates = {key: str(values.get(key, "") or "") for key in MANAGED_ENV_KEYS if key in values}

    rendered_lines: List[str] = []
    seen = set()
    for line in existing_lines:
        parsed = _parse_assignment(line)
        if not parsed:
            rendered_lines.append(line)
            continue
        key, _ = parsed
        if key in updates:
            rendered_lines.append(f"{key}={_format_env_value(updates[key])}")
            seen.add(key)
        else:
            rendered_lines.append(line)

    missing_keys = [key for key in MANAGED_ENV_KEYS if key in updates and key not in seen]
    if missing_keys and rendered_lines and rendered_lines[-1].strip():
        rendered_lines.append("")
    for key in missing_keys:
        rendered_lines.append(f"{key}={_format_env_value(updates[key])}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = path.with_name(f"{path.name}.backup_{timestamp}")
    if path.exists():
        shutil.copy2(path, backup_path)
    else:
        backup_path.write_text("", encoding="utf-8")

    tmp_path = path.with_name(f"{path.name}.tmp_{timestamp}")
    tmp_path.write_text("\n".join(rendered_lines) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)
    return EnvSaveResult(env_path=path, backup_path=backup_path)


def reload_into_environ(env_path: Optional[Path] = None, keys: Optional[Iterable[str]] = None) -> Dict[str, str]:
    path = env_path or default_env_path()
    if load_dotenv:
        load_dotenv(dotenv_path=path, override=True)
    values = read_env_values(path)
    selected_keys = list(keys) if keys is not None else list(values.keys())
    for key in selected_keys:
        if key in values:
            os.environ[key] = values[key]
    return values


def mask_value(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _parse_assignment(line: str) -> Optional[tuple[str, str]]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    key = key.strip()
    if not key:
        return None
    return key, _unquote_env_value(value.strip())


def _unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _format_env_value(value: str) -> str:
    if value == "":
        return ""
    needs_quotes = any(ch.isspace() for ch in value) or "#" in value or value.startswith(("'", '"'))
    if not needs_quotes:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'
