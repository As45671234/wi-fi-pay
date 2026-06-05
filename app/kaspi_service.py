"""
app/kaspi_service.py — Kaspi payment DB ops, sync loop, and activation logic.
"""

import re
import asyncio
import sqlite3
from datetime import datetime, timedelta

from pydantic import BaseModel

from kaspi_client import KaspiApiClient, KaspiClientError

from .config import (
    KASPI_ENABLED, KASPI_API_BASE_URL, KASPI_API_TOKEN,
    KASPI_ORDERS_PATH, KASPI_ORDER_DETAILS_PATH, KASPI_API_TIMEOUT_SECONDS,
    KASPI_SYNC_INTERVAL_SECONDS, KASPI_MATCH_WINDOW_MINUTES,
    KASPI_ACTIVATION_RETRY_DELAY_SECONDS, KASPI_MAX_ACTIVATION_RETRIES,
    KASPI_PAID_STATUSES, KASPI_FAILED_STATUSES, KASPI_PENDING_STATUSES,
    kaspi_sync_stop, kaspi_sync_lock, kaspi_activation_db_lock,
    logger,
)
from .db import get_db
from .utils import _normalize_mac, _is_valid_mac
from .mikrotik import set_mikrotik_ah_access


# ── Pydantic models ────────────────────────────────────────────────────────

class KaspiCreateOrderRequest(BaseModel):
    amount: int
    mac: str
    router_id: str = "astana_01"
    cid: str = ""
    phone: str = ""


class KaspiStatusResponse(BaseModel):
    contract_number: str
    kaspi_status: str
    is_activated: bool
    paid_at: str | None = None
    activated_at: str | None = None
    amount: int
    minutes: int


# ── Result codes ───────────────────────────────────────────────────────────

KASPI_CHECKPAY_RESULT_OK = 0
KASPI_CHECKPAY_RESULT_INVALID_REQUEST = 7
KASPI_CHECKPAY_RESULT_CONTRACT_NOT_FOUND = 5
KASPI_CHECKPAY_RESULT_AMOUNT_MISMATCH = 6
KASPI_CHECKPAY_RESULT_ALREADY_PAID = 8
KASPI_CHECKPAY_RESULT_TX_CONFLICT = 9
KASPI_CHECKPAY_RESULT_INTERNAL_ERROR = 96


# ── Low-level helpers ──────────────────────────────────────────────────────

def _pick_value(data: dict, *keys: str) -> str:
    for key in keys:
        if key in data and data.get(key) is not None:
            value = str(data.get(key)).strip()
            if value:
                return value
    return ""


def _pick_amount_value(data: dict, *keys: str) -> int | None:
    for key in keys:
        if key not in data:
            continue
        raw = data.get(key)
        if raw is None:
            continue
        try:
            return int(float(str(raw).strip()))
        except Exception:
            continue
    return None


def _kaspi_request_id(data: dict) -> str:
    return _pick_value(data, "request_id", "requestId", "id", "request")[:64] or "-"


def _kaspi_response(request_id: str, result: int, message: str, extra: dict | None = None):
    from .utils import utf8_json_response
    payload = {"request_id": request_id, "result": int(result), "message": message}
    if extra:
        payload.update(extra)
    return utf8_json_response(payload)


def _normalize_contract_number(value: str) -> str:
    return (value or "").strip().upper()


def _normalize_mac_kaspi(mac: str) -> str:
    return _normalize_mac(mac)


def make_contract_number(mac: str) -> str:
    import time
    mac_norm = _normalize_mac(mac)
    if not _is_valid_mac(mac_norm):
        raise ValueError("Некорректный MAC")
    mac_hex = mac_norm.replace(":", "").upper()
    ts = int(time.time() * 1000)
    return f"A13{mac_hex}{ts:X}"


def parse_contract_number(contract_number: str) -> tuple[str, bool]:
    raw = (contract_number or "").strip().upper()
    m = re.match(r"A13([0-9A-F]{12})", raw)
    if not m:
        return "", False
    mac_hex = m.group(1)
    mac = ":".join(mac_hex[i:i+2] for i in range(0, 12, 2))
    if not _is_valid_mac(mac):
        return "", False
    return mac, True


def _build_kaspi_client() -> KaspiApiClient:
    return KaspiApiClient(
        base_url=KASPI_API_BASE_URL,
        token=KASPI_API_TOKEN,
        orders_path=KASPI_ORDERS_PATH,
        order_details_path=KASPI_ORDER_DETAILS_PATH,
        timeout_sec=KASPI_API_TIMEOUT_SECONDS,
    )


# ── Auth helper ────────────────────────────────────────────────────────────

def _has_valid_checkpay_auth(request) -> bool:
    import os
    import hmac as _hmac
    token = (os.getenv("KASPI_CHECKPAY_TOKEN") or "").strip()
    if not token:
        return True
    header_api_key = (request.headers.get("x-api-key") or "").strip()
    auth_header = (request.headers.get("authorization") or "").strip()
    bearer = ""
    if auth_header.lower().startswith("bearer "):
        bearer = auth_header[7:].strip()
    return _hmac.compare_digest(header_api_key, token) or _hmac.compare_digest(bearer, token)


# ── DB operations ──────────────────────────────────────────────────────────

def _fetch_kaspi_order_by_contract(contract_number: str) -> sqlite3.Row | None:
    conn = get_db()
    conn.row_factory = sqlite3.Row
    _SQL = """
        SELECT contract_number, kaspi_order_id, kaspi_status, is_activated,
               mac_address, router_id, amount, minutes, paid_at, activated_at
        FROM kaspi_orders
        WHERE contract_number = ?
        ORDER BY id DESC
        LIMIT 1
    """
    try:
        row = conn.execute(_SQL, (contract_number,)).fetchone()
        if row:
            return row
        m = re.match(r"A13([0-9A-F]{12})", contract_number)
        if m:
            mac_hex = m.group(1)
            mac = ":".join(mac_hex[i:i+2] for i in range(0, 12, 2))
            return conn.execute(
                """
                SELECT contract_number, kaspi_order_id, kaspi_status, is_activated,
                       mac_address, router_id, amount, minutes, paid_at, activated_at
                FROM kaspi_orders
                WHERE mac_address = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (mac,),
            ).fetchone()
        return None
    finally:
        conn.close()


def _fetch_contract_by_transaction_id(transaction_id: str) -> str:
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT contract_number
            FROM kaspi_orders
            WHERE kaspi_order_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (transaction_id,),
        ).fetchone()
        return (row[0] if row and row[0] else "") or ""
    finally:
        conn.close()


def _select_local_candidates_for_contract(contract_number: str, amount: int | None) -> list[sqlite3.Row]:
    conn = get_db()
    conn.row_factory = sqlite3.Row
    try:
        args: list = [contract_number]
        sql = "SELECT * FROM kaspi_orders WHERE contract_number = ?"
        if amount is not None:
            sql += " AND amount = ?"
            args.append(amount)
        sql += " ORDER BY created_at DESC LIMIT 3"
        rows = conn.execute(sql, tuple(args)).fetchall()
        return rows
    finally:
        conn.close()


def _upsert_kaspi_remote_state(
    contract_number: str,
    kaspi_order_id: str,
    kaspi_status: str,
    paid_at: str | None,
    phone: str | None = None,
) -> None:
    conn = get_db()
    try:
        conn.execute(
            """
            UPDATE kaspi_orders
            SET kaspi_order_id = CASE
                    WHEN ? <> '' THEN ?
                    ELSE kaspi_order_id
                END,
                kaspi_status = ?,
                paid_at = COALESCE(?, paid_at),
                phone = COALESCE(?, phone),
                updated_at = CURRENT_TIMESTAMP
            WHERE contract_number = ?
            """,
            (kaspi_order_id, kaspi_order_id, kaspi_status, paid_at, phone or None, contract_number),
        )
        conn.commit()
    finally:
        conn.close()


def _claim_kaspi_activation(contract_number: str) -> bool:
    with kaspi_activation_db_lock:
        conn = get_db()
        try:
            row = conn.execute(
                """
                SELECT kaspi_status, is_activated, activation_lock, activation_attempts, last_activation_attempt_at
                FROM kaspi_orders
                WHERE contract_number = ?
                """,
                (contract_number,),
            ).fetchone()
            if not row:
                return False

            status = (row[0] or "").upper()
            is_activated = int(row[1] or 0)
            activation_lock = int(row[2] or 0)
            attempts = int(row[3] or 0)
            last_attempt = row[4]

            if is_activated == 1 or activation_lock == 1:
                return False
            if status not in KASPI_PAID_STATUSES:
                return False
            if attempts >= KASPI_MAX_ACTIVATION_RETRIES:
                return False

            if last_attempt:
                try:
                    last_dt = datetime.fromisoformat(str(last_attempt).replace("Z", "+00:00"))
                    if (datetime.utcnow() - last_dt.replace(tzinfo=None)).total_seconds() < KASPI_ACTIVATION_RETRY_DELAY_SECONDS:
                        return False
                except Exception:
                    pass

            updated = conn.execute(
                """
                UPDATE kaspi_orders
                SET activation_lock = 1,
                    last_activation_attempt_at = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE contract_number = ?
                  AND is_activated = 0
                  AND activation_lock = 0
                """,
                (datetime.utcnow().isoformat(), contract_number),
            ).rowcount
            conn.commit()
            return updated == 1
        finally:
            conn.close()


def _upsert_phone_session_kaspi(contract_number: str) -> None:
    """После успешной активации Kaspi-ордера создаём/обновляем phone_sessions."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT phone, mac_address, router_id, minutes, activated_at FROM kaspi_orders WHERE contract_number=? LIMIT 1",
            (contract_number,),
        ).fetchone()
        if not row or not row[0]:
            return
        phone, mac, router_id, minutes, activated_at_str = row
        if not phone or not mac or not router_id:
            return
        try:
            activated_at = datetime.fromisoformat(str(activated_at_str)) if activated_at_str else datetime.utcnow()
        except Exception:
            activated_at = datetime.utcnow()
        expires_at = activated_at + timedelta(minutes=int(minutes or 60))
        now = datetime.utcnow().isoformat()
        conn.execute(
            """INSERT INTO phone_sessions (phone, mac_address, router_id, expires_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(phone) DO UPDATE SET
                   mac_address=excluded.mac_address,
                   router_id=excluded.router_id,
                   updated_at=excluded.updated_at""",
            (phone, mac, router_id, expires_at.isoformat(), now),
        )
        conn.commit()
        logger.info("[KASPI] phone_session upserted phone=%s*** mac=%s***", phone[:7], mac[:8])
    except Exception as e:
        logger.error("[KASPI] phone_session upsert error contract=%s: %s", contract_number, str(e)[:150])
    finally:
        conn.close()


def _finalize_kaspi_activation(contract_number: str, ok: bool, error_text: str = "") -> None:
    conn = get_db()
    try:
        if ok:
            conn.execute(
                """
                UPDATE kaspi_orders
                SET is_activated = 1,
                    activation_lock = 0,
                    activation_error = NULL,
                    activated_at = COALESCE(activated_at, ?),
                    updated_at = CURRENT_TIMESTAMP
                WHERE contract_number = ?
                """,
                (datetime.utcnow().isoformat(), contract_number),
            )
        else:
            conn.execute(
                """
                UPDATE kaspi_orders
                SET is_activated = 0,
                    activation_lock = 0,
                    activation_attempts = activation_attempts + 1,
                    activation_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE contract_number = ?
                """,
                (error_text[:500], contract_number),
            )
        conn.commit()
    finally:
        conn.close()


def _activate_kaspi_order(contract_number: str) -> bool:
    conn = get_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT mac_address, router_id, minutes, amount
            FROM kaspi_orders
            WHERE contract_number = ?
            """,
            (contract_number,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return False

    mac = row["mac_address"]
    router_id = row["router_id"]
    minutes = int(row["minutes"])

    logger.info("[KASPI] activating contract=%s mac=%s router=%s minutes=%s", contract_number, mac[:8] + "***", router_id, minutes)
    return set_mikrotik_ah_access(mac, router_id, minutes, mode="PAID")


def _process_kaspi_paid(contract_number: str) -> None:
    if not _claim_kaspi_activation(contract_number):
        return
    try:
        ok = _activate_kaspi_order(contract_number)
        if ok:
            _finalize_kaspi_activation(contract_number, ok=True)
            _upsert_phone_session_kaspi(contract_number)
            logger.info("[KASPI] activation done contract=%s", contract_number)
        else:
            _finalize_kaspi_activation(contract_number, ok=False, error_text="Activation failed")
            logger.error("[KASPI] activation failed contract=%s", contract_number)
    except Exception as e:
        _finalize_kaspi_activation(contract_number, ok=False, error_text=str(e))
        logger.error("[KASPI] activation exception contract=%s err=%s", contract_number, str(e)[:200])


def _match_kaspi_order(kaspi_order: dict) -> str | None:
    contract = (kaspi_order.get("contract_number") or "").strip().upper()
    if not contract:
        return None
    amount = kaspi_order.get("amount")
    candidates = _select_local_candidates_for_contract(contract, amount)
    if not candidates:
        return None
    row = candidates[0]
    created_at = row["created_at"]
    try:
        created_dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        created_dt = datetime.utcnow()
    now = datetime.utcnow()
    delta = abs((now - created_dt).total_seconds())
    if delta > KASPI_MATCH_WINDOW_MINUTES * 60:
        return None
    return row["contract_number"]


# ── Sync loop ──────────────────────────────────────────────────────────────

async def kaspi_sync_once() -> dict:
    if not KASPI_ENABLED:
        return {"enabled": False, "reason": "flag_disabled"}

    if not KASPI_API_BASE_URL or not KASPI_API_TOKEN:
        logger.warning("[KASPI] sync skipped, missing API config")
        return {"enabled": True, "reason": "missing_api_config"}

    async with kaspi_sync_lock:
        try:
            client = _build_kaspi_client()
        except KaspiClientError as e:
            logger.error("[KASPI] client init failed: %s", str(e))
            return {"enabled": True, "reason": "client_init_failed"}

        now = datetime.utcnow()
        from_dt = (now - timedelta(minutes=KASPI_MATCH_WINDOW_MINUTES)).isoformat()
        to_dt = now.isoformat()

        statuses = sorted(KASPI_PAID_STATUSES | KASPI_PENDING_STATUSES | KASPI_FAILED_STATUSES)

        try:
            orders = await asyncio.to_thread(client.get_orders, statuses, from_dt, to_dt)
        except Exception as e:
            logger.error("[KASPI] sync error: %s", str(e)[:200])
            return {"enabled": True, "reason": "fetch_failed"}

        matched = 0
        activated = 0

        for ko in orders:
            contract = _match_kaspi_order(ko)
            if not contract:
                continue
            matched += 1
            status = (ko.get("kaspi_status") or "").upper()
            _upsert_kaspi_remote_state(
                contract_number=contract,
                kaspi_order_id=(ko.get("kaspi_order_id") or ""),
                kaspi_status=status,
                paid_at=ko.get("paid_at"),
            )
            if status in KASPI_PAID_STATUSES:
                before = datetime.utcnow()
                _process_kaspi_paid(contract)
                after = datetime.utcnow()
                if (after - before).total_seconds() >= 0:
                    activated += 1

        return {"enabled": True, "orders": len(orders), "matched": matched, "activation_attempted": activated}


async def _kaspi_sync_loop():
    logger.info("[KASPI] sync loop started, interval=%ss", KASPI_SYNC_INTERVAL_SECONDS)
    while not kaspi_sync_stop.is_set():
        try:
            await kaspi_sync_once()
        except Exception as e:
            logger.error("[KASPI] sync loop exception: %s", str(e)[:200])
        try:
            await asyncio.wait_for(kaspi_sync_stop.wait(), timeout=KASPI_SYNC_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
    logger.info("[KASPI] sync loop stopped")
