"""
app/routes/restore.py — Phone-based access recovery.

Flow:
  GET  /restore_access?mac=XX&router_id=YY  → форма ввода телефона
  POST /api/restore_access                  → проверка + применение binding к новому MAC
"""

import asyncio
from datetime import datetime, timedelta

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse

from ..config import ROUTERS_CONFIG, MIKROTIK_EXECUTOR, templates, logger
from ..db import get_db
from ..utils import _normalize_phone, _is_valid_mac, utf8_json_response
from ..mikrotik import set_mikrotik_ah_access, remove_mac_binding, grant_driver_access

router = APIRouter()


def _get_active_session(phone: str, now: datetime):
    """Ищет активную сессию по телефону: сначала phone_sessions, затем kaspi_orders."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT mac_address, router_id, expires_at FROM phone_sessions WHERE phone=? AND expires_at > datetime('now')",
            (phone,),
        ).fetchone()
        if row:
            return row[0], row[1], datetime.fromisoformat(row[2])

        # Fallback: Kaspi-ордера где телефон сохранён Kaspi-ом при check/pay
        kaspi = conn.execute(
            """SELECT mac_address, router_id, minutes, activated_at
               FROM kaspi_orders
               WHERE phone=? AND is_activated=1 AND activated_at IS NOT NULL
               ORDER BY activated_at DESC LIMIT 1""",
            (phone,),
        ).fetchone()
        if kaspi:
            # kaspi = (mac_address, router_id, minutes, activated_at)
            try:
                activated_at = datetime.fromisoformat(str(kaspi[3]))
            except Exception:
                activated_at = now
            expires_at = activated_at + timedelta(minutes=int(kaspi[2] or 60))
            if expires_at > now:
                return kaspi[0], kaspi[1], expires_at
    finally:
        conn.close()
    return None, None, None


def _get_driver_binding(phone: str):
    """Возвращает (mac, router_id) для телефона водителя из белого списка, иначе (None, None)."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT mac_address, router_id FROM driver_phones WHERE phone=?",
            (phone,),
        ).fetchone()
    finally:
        conn.close()
    if row:
        return row[0], row[1]
    return None, None


def _update_driver_binding(phone: str, mac: str, router_id: str):
    now = datetime.utcnow().isoformat()
    conn = get_db()
    try:
        conn.execute(
            "UPDATE driver_phones SET mac_address=?, router_id=?, updated_at=? WHERE phone=?",
            (mac, router_id, now, phone),
        )
        conn.commit()
    finally:
        conn.close()


def _upsert_phone_session(phone: str, mac: str, router_id: str, expires_at: datetime):
    now = datetime.utcnow().isoformat()
    conn = get_db()
    try:
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
    finally:
        conn.close()


@router.get("/restore_access")
async def restore_access_page(
    request: Request,
    mac: str = "00:00:00:00:00:00",
    router_id: str = "astana_01",
    cid: str = "",
    error: str = "",
):
    return templates.TemplateResponse(
        "restore_access.html",
        {"request": request, "mac": mac, "router_id": router_id, "cid": cid, "error": error},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/restore_access")
async def api_restore_access(
    request: Request,
    phone: str = Form(...),
    mac: str = Form(...),
    router_id: str = Form(...),
    cid: str = Form(""),
):
    phone_norm = _normalize_phone(phone)
    error_redirect = f"/restore_access?mac={mac}&router_id={router_id}&cid={cid}&error="

    if not phone_norm:
        return RedirectResponse(url=error_redirect + "Некорректный номер телефона", status_code=303)

    if not _is_valid_mac(mac):
        return RedirectResponse(url=error_redirect + "Ошибка сессии: некорректный MAC", status_code=303)

    if router_id not in ROUTERS_CONFIG:
        return RedirectResponse(url=error_redirect + "Ошибка сессии: неизвестный роутер", status_code=303)

    loop = asyncio.get_running_loop()

    # Водители: бессрочный доступ, но только на СВОЁМ роутере (автобусе).
    # На чужих роутерах водитель — обычный клиент: платит и попадает в
    # phone_sessions/kaspi_orders как все, без каких-либо привилегий.
    driver_old_mac, driver_old_router = _get_driver_binding(phone_norm)
    if driver_old_mac and driver_old_router == router_id:
        if driver_old_mac != mac:
            logger.info("[restore] DRIVER MAC change %s***→%s*** router %s→%s phone=%s***",
                        driver_old_mac[:8], mac[:8], driver_old_router, router_id, phone_norm[:7])
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(MIKROTIK_EXECUTOR, remove_mac_binding, driver_old_mac, driver_old_router),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                logger.warning("[restore] DRIVER remove_mac_binding timeout mac=%s*** router=%s",
                               driver_old_mac[:8], driver_old_router)

        try:
            ok = await asyncio.wait_for(
                loop.run_in_executor(MIKROTIK_EXECUTOR, grant_driver_access, mac, router_id),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            ok = False
        if not ok:
            logger.error("[restore] DRIVER activation failed mac=%s*** router=%s phone=%s***", mac[:8], router_id, phone_norm[:7])
            return RedirectResponse(
                url=error_redirect + "Не удалось применить доступ. Попробуйте ещё раз.",
                status_code=303,
            )

        _update_driver_binding(phone_norm, mac, router_id)
        logger.info("[restore] DRIVER SUCCESS phone=%s*** mac=%s*** router=%s", phone_norm[:7], mac[:8], router_id)
        return RedirectResponse(
            url=f"/success?mac={mac}&router_id={router_id}&minutes=0&amount=0&payment_method=driver",
            status_code=303,
        )

    now = datetime.utcnow()
    old_mac, old_router, expires_at = _get_active_session(phone_norm, now)

    if not old_mac:
        logger.info("[restore] not found phone=%s*** mac=%s*** router=%s", phone_norm[:7], mac[:8], router_id)
        return RedirectResponse(
            url=error_redirect + "Активная оплата не найдена. Проверьте номер телефона.",
            status_code=303,
        )

    remaining_minutes = max(1, int((expires_at - now).total_seconds() / 60))

    # One-device: отзываем старый биндинг если MAC сменился
    if old_mac != mac:
        logger.info("[restore] MAC change %s***→%s*** router %s→%s phone=%s***",
                    old_mac[:8], mac[:8], old_router, router_id, phone_norm[:7])
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(MIKROTIK_EXECUTOR, remove_mac_binding, old_mac, old_router)

    # Применяем доступ к новому MAC
    loop = asyncio.get_running_loop()
    ok = await asyncio.wait_for(
        loop.run_in_executor(MIKROTIK_EXECUTOR, set_mikrotik_ah_access, mac, router_id, remaining_minutes, "PAID"),
        timeout=15.0,
    )

    if not ok:
        logger.error("[restore] activation failed mac=%s*** router=%s phone=%s***", mac[:8], router_id, phone_norm[:7])
        return RedirectResponse(
            url=error_redirect + "Не удалось применить доступ. Попробуйте ещё раз.",
            status_code=303,
        )

    _upsert_phone_session(phone_norm, mac, router_id, expires_at)
    logger.info("[restore] SUCCESS phone=%s*** mac=%s*** router=%s remaining=%dmin",
                phone_norm[:7], mac[:8], router_id, remaining_minutes)

    return RedirectResponse(
        url=f"/success?mac={mac}&router_id={router_id}&minutes={remaining_minutes}&amount=0&payment_method=restore",
        status_code=303,
    )
