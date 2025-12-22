from __future__ import annotations

import email
import imaplib
import logging
import os
import re
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from services.catalog_service import DEFAULT_CATALOG_DB_PATH
from services.db import (
    CATALOG_DB_PATH,
    df_remittances_get_imported_message_ids,
    df_remittances_insert_many,
    ensure_df_remittances_table,
    get_app_kv,
    get_db_connection_for_path,
)

LOGGER = logging.getLogger(__name__)
DEFAULT_MAX_MESSAGES = 200
DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_MAILBOX = "[Gmail]/All Mail"


def _get_config_value(key: str, default: Optional[str] = None, *, db_path: Path) -> Optional[str]:
    env_val = os.getenv(key)
    if env_val is not None and str(env_val).strip() != "":
        return str(env_val).strip()

    # Only consult app_kv_store when using the primary catalog DB to avoid touching test DBs.
    if Path(db_path).resolve() != Path(CATALOG_DB_PATH).resolve():
        return default

    try:
        with get_db_connection_for_path(db_path) as conn:
            value = get_app_kv(conn, key)
            if value is not None and str(value).strip() != "":
                return str(value).strip()
    except Exception as exc:  # pragma: no cover - defensive logging only
        LOGGER.debug("[DF Remittances] Failed to read %s from app_kv_store: %s", key, exc)
    return default


def _extract_gm_msgid(data: bytes) -> Optional[str]:
    match = re.search(rb"X-GM-MSGID\s+(\d+)", data or b"")
    return match.group(1).decode() if match else None


def _extract_message(fetch_data: List[Tuple[bytes, bytes]]) -> Tuple[Optional[str], Optional[Message]]:
    gmail_id: Optional[str] = None
    raw: Optional[bytes] = None
    for chunk in fetch_data:
        if not isinstance(chunk, tuple) or len(chunk) < 2:
            continue
        header_bytes = chunk[0] or b""
        if gmail_id is None:
            gmail_id = _extract_gm_msgid(header_bytes)
        if chunk[1]:
            raw = chunk[1]
    if raw is None:
        return gmail_id, None
    try:
        msg = email.message_from_bytes(raw)
    except Exception:
        msg = None
    return gmail_id, msg


def _strip_html(html: str) -> str:
    text = re.sub(r"<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _get_plain_body(msg: Optional[Message]) -> str:
    if msg is None:
        return ""

    # Prefer text/plain parts without attachments.
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if ctype == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                html = payload.decode(charset, errors="replace")
                return _strip_html(html)

    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except Exception:
        return payload.decode(errors="replace") if payload else ""


def parse_remittance_email_body(body: str) -> List[Dict[str, object]]:
    """
    Parse a remittance email body using the GAS-compatible rules.
    Returns a list of row dicts ready for insertion (without gmail_message_id).
    """
    if body is None:
        return []

    lines = re.split(r"\r?\n", body)
    remittance_id = None
    payment_date = None
    payment_currency = None

    for line in lines:
        normalized = line.strip()
        if re.match(r"^payment\s+number\s*:\s*", normalized, flags=re.IGNORECASE):
            remittance_id = normalized.split(":", 1)[1].strip() if ":" in normalized else ""
        elif re.match(r"^payment\s+date\s*:\s*", normalized, flags=re.IGNORECASE):
            payment_date = normalized.split(":", 1)[1].strip() if ":" in normalized else ""
        elif re.match(r"^payment\s+currency\s*:\s*", normalized, flags=re.IGNORECASE):
            payment_currency = normalized.split(":", 1)[1].strip() if ":" in normalized else ""

    if not payment_currency:
        payment_currency = "AED"

    rows: List[Dict[str, object]] = []
    invoice_pattern = re.compile(r"^[A-Za-z0-9]+$")
    invoice_date_pattern = re.compile(r"^\d{2}-[A-Z]{3}-\d{4}$", flags=re.IGNORECASE)

    for line in lines:
        tokens = [tok for tok in re.split(r"\s+", line.strip()) if tok]
        if len(tokens) < 4:
            continue

        invoice_number = tokens[0]
        if not invoice_pattern.match(invoice_number):
            continue

        invoice_date = tokens[1]
        if not invoice_date_pattern.match(invoice_date):
            continue

        description = tokens[2] if len(tokens) >= 3 else ""
        if len(tokens) >= 6:
            amount_paid_raw = tokens[-2]
        elif len(tokens) == 5:
            amount_paid_raw = tokens[3]
        else:
            continue

        try:
            paid_amount = float(str(amount_paid_raw).replace(",", ""))
        except Exception:
            paid_amount = 0.0

        purchase_order_number = description.split("/")[0] if description else ""

        rows.append(
            {
                "invoice_number": invoice_number,
                "purchase_order_number": purchase_order_number,
                "payment_date": payment_date or "",
                "paid_amount": paid_amount,
                "currency": payment_currency or "AED",
                "remittance_id": remittance_id or "",
                "gmail_message_id": None,
                "imported_at_utc": None,
            }
        )

    return rows


def import_df_remittances_from_gmail(
    *,
    db_path: Path = DEFAULT_CATALOG_DB_PATH,
    max_messages: Optional[int] = None,
    label: Optional[str] = None,
    mailbox: Optional[str] = None,
) -> Dict[str, object]:
    """Fetch Gmail remittances via IMAP (Gmail extensions) and persist them."""
    ensure_df_remittances_table(db_path)

    imap_host = _get_config_value("DF_REMITTANCE_IMAP_HOST", DEFAULT_IMAP_HOST, db_path=db_path)
    imap_user = _get_config_value("DF_REMITTANCE_IMAP_USER", None, db_path=db_path)
    imap_pass = _get_config_value("DF_REMITTANCE_IMAP_PASS", None, db_path=db_path)
    gmail_label = label or _get_config_value("DF_REMITTANCE_GMAIL_LABEL", None, db_path=db_path)
    mailbox_name = mailbox or _get_config_value("DF_REMITTANCE_IMAP_MAILBOX", DEFAULT_MAILBOX, db_path=db_path)
    cap_raw = max_messages if max_messages is not None else _get_config_value(
        "DF_REMITTANCE_MAX_MESSAGES", str(DEFAULT_MAX_MESSAGES), db_path=db_path
    )
    try:
        cap = int(cap_raw)
    except Exception:
        cap = DEFAULT_MAX_MESSAGES
    cap = max(1, cap)

    missing = [k for k, v in {
        "DF_REMITTANCE_IMAP_USER": imap_user,
        "DF_REMITTANCE_IMAP_PASS": imap_pass,
        "DF_REMITTANCE_GMAIL_LABEL": gmail_label,
    }.items() if not v]
    if missing:
        return {"status": "disabled", "reason": "missing_config", "missing": missing}

    existing_ids = df_remittances_get_imported_message_ids(limit=5000, db_path=db_path)
    parsed_rows: List[Dict[str, object]] = []
    processed_msgs = 0
    skipped_existing = 0

    imap = None
    try:
        imap = imaplib.IMAP4_SSL(imap_host)
        imap.login(imap_user, imap_pass)
        status, _ = imap.select(mailbox_name, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Failed to select mailbox {mailbox_name}")

        status, data = imap.search(None, "X-GM-LABELS", f'"{gmail_label}"')
        if status != "OK":
            raise RuntimeError(f"Failed to search for label {gmail_label}")

        msg_nums = data[0].split() if data and data[0] else []
        if not msg_nums:
            return {
                "status": "ok",
                "messages_found": 0,
                "messages_processed": 0,
                "rows_parsed": 0,
                "rows_inserted": 0,
                "skipped_existing": 0,
                "label": gmail_label,
            }

        capped = list(reversed(msg_nums[-max(cap, 1):]))
        for num in capped:
            status, msg_data = imap.fetch(num, "(X-GM-MSGID BODY.PEEK[])")
            if status != "OK" or not msg_data:
                continue

            gmail_id, msg = _extract_message(msg_data)
            if not gmail_id:
                continue
            if gmail_id in existing_ids:
                skipped_existing += 1
                continue

            body = _get_plain_body(msg)
            rows = parse_remittance_email_body(body)
            if not rows:
                continue

            imported_at = datetime.now(timezone.utc).isoformat()
            for row in rows:
                row["gmail_message_id"] = gmail_id
                row["imported_at_utc"] = imported_at
            parsed_rows.extend(rows)
            existing_ids.add(gmail_id)
            processed_msgs += 1
    finally:
        try:
            if imap:
                imap.logout()
        except Exception:
            pass

    inserted = df_remittances_insert_many(parsed_rows, db_path=db_path)
    LOGGER.info(
        "[DF Remittances] Gmail import | label=%s | mailbox=%s | found=%s | processed=%s | rows_parsed=%s | inserted=%s | skipped_existing=%s",
        gmail_label,
        mailbox_name,
        len(msg_nums) if "msg_nums" in locals() else 0,
        processed_msgs,
        len(parsed_rows),
        inserted,
        skipped_existing,
    )

    return {
        "status": "imported" if inserted else "ok",
        "messages_found": len(msg_nums) if "msg_nums" in locals() else 0,
        "messages_processed": processed_msgs,
        "rows_parsed": len(parsed_rows),
        "rows_inserted": inserted,
        "skipped_existing": skipped_existing,
        "label": gmail_label,
    }
