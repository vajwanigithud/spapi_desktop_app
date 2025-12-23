from __future__ import annotations

import email
import html
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
    text = re.sub(r"<\s*br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*tr\b", "\n<tr", text, flags=re.IGNORECASE)
    text = re.sub(r"</\s*tr\s*>", "</tr>\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</\s*td\s*>\s*<\s*td\b", "</td>\t<td", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text, flags=re.DOTALL)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


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
        body = payload.decode(charset, errors="replace")
    except Exception:
        body = payload.decode(errors="replace") if payload else ""

    if (msg.get_content_type() or "").lower() == "text/html":
        return _strip_html(body)
    return body


def _clean_html_cell(cell: str) -> str:
    text = re.sub(r"<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>", " ", cell, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return html.unescape(text)


def _parse_html_remittance(body: str) -> List[Dict[str, object]]:
    cells = [_clean_html_cell(m) for m in re.findall(r"<td[^>]*>(.*?)</td>", body, flags=re.IGNORECASE | re.DOTALL)]
    remittance_id = None
    payment_date = None
    payment_currency = None

    for idx, label in enumerate(cells):
        lowered = label.lower()
        if lowered.startswith("payment number") and idx + 1 < len(cells):
            remittance_id = cells[idx + 1].strip()
        elif lowered.startswith("payment date") and idx + 1 < len(cells):
            payment_date = cells[idx + 1].strip()
        elif lowered.startswith("payment currency") and idx + 1 < len(cells):
            payment_currency = cells[idx + 1].strip()

    if not payment_currency:
        payment_currency = "AED"

    rows: List[Dict[str, object]] = []
    for tr_content in re.findall(r"<tr[^>]*>(.*?)</tr>", body, flags=re.IGNORECASE | re.DOTALL):
        td_values = [_clean_html_cell(m) for m in re.findall(r"<td[^>]*>(.*?)</td>", tr_content, flags=re.IGNORECASE | re.DOTALL)]
        if not td_values:
            continue

        lowered_cells = [c.lower() for c in td_values]
        if any(lbl.startswith(prefix) for lbl in lowered_cells for prefix in ("payment number", "payment date", "payment currency")):
            continue
        if any("invoice number" in lbl for lbl in lowered_cells):
            continue
        if len(td_values) < 4:
            continue

        invoice_number = td_values[0].strip()
        if not invoice_number:
            continue

        description = td_values[2].strip() if len(td_values) >= 3 else ""
        paid_raw = td_values[-2].strip() if len(td_values) >= 2 else ""
        paid_clean = re.sub(r"[ ,]", "", paid_raw) if paid_raw else ""
        paid_clean = re.sub(r"[^0-9.\-]", "", paid_clean)
        try:
            paid_amount = float(paid_clean) if paid_clean else 0.0
        except Exception:
            paid_amount = 0.0

        purchase_order_number = description.split("/")[0].strip() if description else ""

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


def parse_remittance_email_body(body: str) -> List[Dict[str, object]]:
    """
    Parse a remittance email body using the GAS-compatible rules.
    Returns a list of row dicts ready for insertion (without gmail_message_id).
    """
    if body is None:
        return []

    if re.search(r"<\s*(table|tr|td)\b", body, flags=re.IGNORECASE):
        html_rows = _parse_html_remittance(body)
        if html_rows:
            return html_rows

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
    force: bool = False,
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

    LOGGER.info(
        "[DF Remittances] Import start | host=%s | mailbox=%s | label=%s | cap=%s | user_present=%s | pass_present=%s",
        imap_host,
        mailbox_name,
        gmail_label,
        cap,
        bool(imap_user),
        bool(imap_pass),
    )

    missing = [k for k, v in {
        "DF_REMITTANCE_IMAP_USER": imap_user,
        "DF_REMITTANCE_IMAP_PASS": imap_pass,
        "DF_REMITTANCE_GMAIL_LABEL": gmail_label,
    }.items() if not v]
    if missing:
        return {"status": "disabled", "reason": "missing_config", "missing": missing}

    existing_ids = set(df_remittances_get_imported_message_ids(limit=5000, db_path=db_path))
    parsed_rows: List[Dict[str, object]] = []
    processed_msgs = 0
    skipped_existing = 0

    imap = None
    stage = "connect"
    try:
        imap = imaplib.IMAP4_SSL(imap_host)
        stage = "login"
        imap.login(imap_user, imap_pass)
        LOGGER.info("[DF Remittances] IMAP login ok | host=%s", imap_host)

        stage = "select"
        status, _ = imap.select(mailbox_name, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Failed to select mailbox {mailbox_name}")
        LOGGER.info("[DF Remittances] IMAP select ok | mailbox=%s", mailbox_name)

        stage = "search"
        status, data = imap.search(None, "X-GM-LABELS", f'"{gmail_label}"')
        if status != "OK":
            raise RuntimeError(f"Failed to search for label {gmail_label}")

        msg_nums = data[0].split() if data and data[0] else []
        LOGGER.info("[DF Remittances] Search ok | messages_found=%s", len(msg_nums))
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
        LOGGER.info(
            "[DF Remittances] Fetch loop start | total=%s | capped=%s",
            len(msg_nums),
            len(capped),
        )
        for num in capped:
            status, msg_data = imap.fetch(num, "(X-GM-MSGID BODY.PEEK[])")
            if status != "OK" or not msg_data:
                continue

            gmail_id, msg = _extract_message(msg_data)
            if not gmail_id:
                continue
            if gmail_id in existing_ids and not force:
                skipped_existing += 1
                continue

            body = _get_plain_body(msg)
            rows = parse_remittance_email_body(body)
            if not rows:
                continue

            if LOGGER.isEnabledFor(logging.DEBUG):
                LOGGER.debug(
                    "[DF Remittances] Parsed rows preview | gmail_id=%s | rows=%s",
                    gmail_id,
                    rows[:2],
                )

            imported_at = datetime.now(timezone.utc).isoformat()
            for row in rows:
                row["gmail_message_id"] = gmail_id
                row["imported_at_utc"] = imported_at
            parsed_rows.extend(rows)
            existing_ids.add(gmail_id)
            processed_msgs += 1
    except Exception as exc:
        LOGGER.error("[DF Remittances] IMAP %s failed: %s", stage, exc, exc_info=True)
        return {"status": "error", "reason": "imap_error", "stage": stage, "message": str(exc)}
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
