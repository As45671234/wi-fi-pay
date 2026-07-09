"""
app/routes/driver.py — Driver whitelist provisioning (замена grant_permanent_access.sh).

Flow (два шага, пароль отдельно от формы):
  GET  /driver_access               → шаг 1: только пароль
  POST /driver_access               → проверка пароля → шаг 2: роутер (id) + телефон + комментарий
  POST /api/driver_access           → повторная проверка пароля, лимит 3 телефона на роутер,
                                       автоопределение MAC на роутере, выдача бессрочного доступа,
                                       запись в driver_phones.

Дальнейшая смена устройства водителем — самостоятельно через /restore_access
(тот же номер телефона), без участия админа.
"""

import asyncio
import hmac as _hmac
from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse

from ..config import ROUTERS_CONFIG, DRIVER_ACCESS_PASSWORD, MIKROTIK_EXECUTOR, templates, logger
from ..db import get_db
from ..utils import _normalize_phone
from ..mikrotik import grant_driver_access, _pick_qr_mac_fallback
from ..pending import _get_busy_activation_macs

router = APIRouter()

_MAC_DETECT_TIMEOUT_SECONDS = 5.0
_MAX_DRIVERS_PER_ROUTER = 3


def _check_password(password: str) -> bool:
    return bool(DRIVER_ACCESS_PASSWORD) and _hmac.compare_digest((password or "").strip(), DRIVER_ACCESS_PASSWORD)


def _router_driver_counts() -> dict:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT router_id, COUNT(*) FROM driver_phones GROUP BY router_id"
        ).fetchall()
    finally:
        conn.close()
    return {row[0]: row[1] for row in rows}


def _render_form(request: Request, password: str, router_id: str = "", phone: str = "", note: str = "", error: str = ""):
    counts = _router_driver_counts()
    routers = [
        {"id": rid, "count": counts.get(rid, 0), "full": counts.get(rid, 0) >= _MAX_DRIVERS_PER_ROUTER}
        for rid in sorted(ROUTERS_CONFIG.keys())
    ]
    return templates.TemplateResponse(
        "driver_access.html",
        {
            "request": request,
            "password": password,
            "routers": routers,
            "max_drivers": _MAX_DRIVERS_PER_ROUTER,
            "router_id": router_id,
            "phone": phone,
            "note": note,
            "error": error,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/driver_access")
async def driver_login_page(request: Request, error: str = ""):
    return templates.TemplateResponse(
        "driver_login.html",
        {"request": request, "error": error},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/driver_access")
async def driver_login_submit(request: Request, password: str = Form(...)):
    if not _check_password(password):
        logger.warning("[driver_access] неверный пароль (шаг 1)")
        return RedirectResponse(url=f"/driver_access?{urlencode({'error': 'Неверный пароль'})}", status_code=303)
    return _render_form(request, password)


@router.post("/api/driver_access")
async def api_driver_access(
    request: Request,
    password: str = Form(...),
    router_id: str = Form(...),
    phone: str = Form(...),
    note: str = Form(""),
):
    if not _check_password(password):
        logger.warning("[driver_access] неверный пароль (шаг 2) router=%s", router_id)
        return RedirectResponse(url=f"/driver_access?{urlencode({'error': 'Неверный пароль'})}", status_code=303)

    phone_norm = _normalize_phone(phone)
    if not phone_norm:
        return _render_form(request, password, router_id, phone, note, "Некорректный номер телефона")

    if router_id not in ROUTERS_CONFIG:
        return _render_form(request, password, router_id, phone, note, "Неизвестный роутер")

    conn = get_db()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM driver_phones WHERE router_id=? AND phone<>?",
            (router_id, phone_norm),
        ).fetchone()[0]
    finally:
        conn.close()
    if count >= _MAX_DRIVERS_PER_ROUTER:
        return _render_form(
            request, password, router_id, phone, note,
            f"На роутере {router_id} уже {_MAX_DRIVERS_PER_ROUTER} водителя(ей) — лимит достигнут. "
            "Освободите слот или выберите другой роутер.",
        )

    busy_macs = _get_busy_activation_macs(router_id)
    loop = asyncio.get_running_loop()
    try:
        mac, reason = await asyncio.wait_for(
            loop.run_in_executor(MIKROTIK_EXECUTOR, _pick_qr_mac_fallback, router_id, busy_macs),
            timeout=_MAC_DETECT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        mac, reason = None, "router_timeout"
    except Exception:
        mac, reason = None, "router_error"

    if not mac:
        logger.info("[driver_access] mac not found router=%s reason=%s phone=%s***", router_id, reason, phone_norm[:7])
        return _render_form(
            request, password, router_id, phone, note,
            "Не удалось определить устройство. Убедитесь, что телефон водителя "
            "подключён к WiFi этого автобуса (и больше никто не подключён), и повторите.",
        )

    ok = await asyncio.wait_for(
        loop.run_in_executor(MIKROTIK_EXECUTOR, grant_driver_access, mac, router_id),
        timeout=15.0,
    )
    if not ok:
        logger.error("[driver_access] grant failed mac=%s*** router=%s", mac[:8], router_id)
        return _render_form(request, password, router_id, phone, note, "Не удалось выдать доступ. Попробуйте ещё раз.")

    now = datetime.utcnow().isoformat()
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO driver_phones (phone, router_id, mac_address, note, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(phone) DO UPDATE SET
                   router_id=excluded.router_id,
                   mac_address=excluded.mac_address,
                   note=excluded.note,
                   updated_at=excluded.updated_at""",
            (phone_norm, router_id, mac, note.strip()[:200], now),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info("[driver_access] SUCCESS phone=%s*** mac=%s*** router=%s", phone_norm[:7], mac[:8], router_id)

    return RedirectResponse(
        url=f"/success?{urlencode({'mac': mac, 'router_id': router_id, 'minutes': 0, 'amount': 0, 'payment_method': 'driver'})}",
        status_code=303,
    )
