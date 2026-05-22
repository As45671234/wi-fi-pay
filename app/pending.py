"""
app/pending.py — Pending activation queue: enqueue, claim, finalize, drain, loop.
"""

import asyncio
import sqlite3
from datetime import datetime, timedelta

from .config import (
    ROUTERS_CONFIG, PENDING_ACTIVATION_MAX_ATTEMPTS,
    PENDING_ACTIVATION_RETRY_DELAY_SECONDS,
    PENDING_ACTIVATION_LOOP_INTERVAL_SECONDS,
    pending_activation_stop, logger,
)
from .db import get_db
from .utils import _normalize_mac, _is_valid_mac
from .mikrotik import set_mikrotik_ah_access


def _enqueue_pending_activation(
    router_id: str, mac: str, amount: int, minutes: int, payment_order_id: str = ""
) -> int:
    mac_norm = _normalize_mac(mac)
    if not _is_valid_mac(mac_norm):
        raise ValueError("Некорректный MAC")
    if router_id not in ROUTERS_CONFIG:
        raise ValueError("Неизвестный роутер")

    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT id FROM pending_activations
            WHERE router_id = ?
              AND mac_address = ?
              AND status IN ('PENDING', 'PROCESSING', 'RETRY')
              AND created_at > datetime('now', '-30 minutes')
            ORDER BY id DESC
            LIMIT 1
            """,
            (router_id, mac_norm),
        ).fetchone()
        if row:
            return int(row[0])

        cur = conn.execute(
            """
            INSERT INTO pending_activations (
                router_id, mac_address, amount, minutes, payment_order_id, status,
                attempts, next_retry_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'PENDING', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (router_id, mac_norm, int(amount), int(minutes), payment_order_id or ""),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _claim_next_pending_activation() -> sqlite3.Row | None:
    conn = get_db()
    conn.row_factory = sqlite3.Row
    try:
        candidates = conn.execute(
            """
            SELECT id, router_id, mac_address, amount, minutes, payment_order_id, status, attempts
            FROM pending_activations
            WHERE status IN ('PENDING', 'RETRY')
              AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP)
            ORDER BY created_at ASC
            LIMIT 10
            """
        ).fetchall()

        for row in candidates:
            updated = conn.execute(
                """
                UPDATE pending_activations
                SET status = 'PROCESSING',
                    last_attempt_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                  AND status = ?
                """,
                (int(row["id"]), row["status"]),
            ).rowcount
            if updated == 1:
                conn.commit()
                return row

        conn.commit()
        return None
    finally:
        conn.close()


def _finalize_pending_activation(row_id: int, ok: bool, error_text: str = "") -> None:
    conn = get_db()
    try:
        if ok:
            conn.execute(
                """
                UPDATE pending_activations
                SET status = 'DONE',
                    activated_at = CURRENT_TIMESTAMP,
                    last_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (int(row_id),),
            )
        else:
            row = conn.execute(
                "SELECT attempts FROM pending_activations WHERE id = ?", (int(row_id),)
            ).fetchone()
            attempts = int((row[0] if row else 0) or 0) + 1
            failed = attempts >= PENDING_ACTIVATION_MAX_ATTEMPTS
            next_retry_expr = f"+{PENDING_ACTIVATION_RETRY_DELAY_SECONDS} seconds"
            conn.execute(
                """
                UPDATE pending_activations
                SET status = ?,
                    attempts = ?,
                    last_error = ?,
                    next_retry_at = CASE WHEN ? = 'FAILED' THEN NULL ELSE datetime('now', ?) END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    "FAILED" if failed else "RETRY",
                    attempts,
                    (error_text or "Activation failed")[:500],
                    "FAILED" if failed else "RETRY",
                    next_retry_expr,
                    int(row_id),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _mark_order_paid_from_activation(
    router_id: str, mac: str, amount: int, minutes: int, payment_order_id: str = ""
) -> None:
    paid_expires_at = (datetime.utcnow() + timedelta(minutes=int(minutes))).isoformat()
    conn = get_db()
    try:
        updated = 0
        if payment_order_id:
            updated = conn.execute(
                """
                UPDATE orders
                SET status = 'PAID', amount = ?, mac_address = ?, router_id = ?, expires_at = ?
                WHERE payment_order_id = ?
                """,
                (int(amount), mac, router_id, paid_expires_at, payment_order_id),
            ).rowcount

        if not updated:
            updated = conn.execute(
                """
                UPDATE orders
                SET status = 'PAID', amount = ?, router_id = ?, expires_at = ?
                WHERE id = (
                    SELECT id FROM orders
                    WHERE mac_address = ?
                      AND router_id = ?
                      AND status IN ('PAYMENT_INITIATED', 'PAY_WINDOW', 'PAYMENT_CONFIRMED', 'PAID')
                    ORDER BY id DESC
                    LIMIT 1
                )
                """,
                (int(amount), router_id, paid_expires_at, mac, router_id),
            ).rowcount

        if not updated:
            conn.execute(
                """
                INSERT INTO orders (mac_address, amount, status, router_id, payment_order_id, expires_at)
                VALUES (?, ?, 'PAID', ?, ?, ?)
                """,
                (mac, int(amount), router_id, payment_order_id or "", paid_expires_at),
            )
        conn.commit()
    finally:
        conn.close()


def _process_one_pending_activation() -> bool:
    row = _claim_next_pending_activation()
    if not row:
        return False

    row_id = int(row["id"])
    router_id = row["router_id"]
    mac = row["mac_address"]
    amount = int(row["amount"] or 0)
    minutes = int(row["minutes"] or 60)
    payment_order_id = row["payment_order_id"] or ""

    try:
        ok = set_mikrotik_ah_access(mac, router_id, minutes, mode="PAID")
        if ok:
            _mark_order_paid_from_activation(router_id, mac, amount, minutes, payment_order_id)
            _finalize_pending_activation(row_id, ok=True)
            logger.info("[PENDING_ACTIVATION] done id=%s mac=%s router=%s", row_id, mac[:8] + "***", router_id)
        else:
            _finalize_pending_activation(row_id, ok=False, error_text="Router activation returned false")
            logger.error("[PENDING_ACTIVATION] failed id=%s mac=%s router=%s", row_id, mac[:8] + "***", router_id)
    except Exception as e:
        _finalize_pending_activation(row_id, ok=False, error_text=str(e))
        logger.error("[PENDING_ACTIVATION] exception id=%s err=%s", row_id, str(e)[:200])

    return True


async def _drain_pending_activations(limit: int = 1) -> int:
    processed = 0
    for _ in range(max(1, int(limit))):
        ok = await asyncio.to_thread(_process_one_pending_activation)
        if not ok:
            break
        processed += 1
    return processed


async def _pending_activation_loop():
    logger.info("[PENDING_ACTIVATION] loop started, interval=%ss", PENDING_ACTIVATION_LOOP_INTERVAL_SECONDS)
    while not pending_activation_stop.is_set():
        try:
            await _drain_pending_activations(limit=8)
        except Exception as e:
            logger.error("[PENDING_ACTIVATION] loop exception: %s", str(e)[:200])
        try:
            await asyncio.wait_for(pending_activation_stop.wait(), timeout=PENDING_ACTIVATION_LOOP_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
    logger.info("[PENDING_ACTIVATION] loop stopped")


def _get_busy_activation_macs(router_id: str) -> set[str]:
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT mac_address
            FROM pending_activations
            WHERE router_id = ?
              AND status IN ('PENDING', 'PROCESSING', 'RETRY')
              AND created_at > datetime('now', '-30 minutes')
            """,
            (router_id,),
        ).fetchall()
        return {_normalize_mac(r[0]) for r in rows if r and r[0]}
    finally:
        conn.close()
