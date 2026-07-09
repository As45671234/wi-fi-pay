"""
app/routes/driver.py — Driver whitelist provisioning (замена grant_permanent_access.sh).

Flow:
  GET  /driver_access               → форма: пароль + роутер (id) + телефон водителя
  POST /api/driver_access           → проверка пароля, автоопределение MAC на роутере,
                                       выдача бессрочного доступа, запись в driver_phones.

Дальнейшая смена устройства водителем — самостоятельно через /restore_access
(тот же номер телефона), без участия админа.
"""

import asyncio
import hmac as _hmac
from datetime import datetime

from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse

from ..config import ROUTERS_CONFIG, DRIVER_ACCESS_PASSWORD, MIKROTIK_EXECUTOR, templates, logger
from ..db import get_db
from ..utils import _normalize_phone
from ..mikrotik import grant_driver_access, _pick_qr_mac_fallback
from ..pending import _get_busy_activation_macs

router = APIRouter()

_MAC_DETECT_TIMEOUT_SECONDS = 5.0


@router.get("/driver_access")
async def driver_access_page(
    request: Request,
    router_id: str = "",
    phone: str = "",
    error: str = "",
):
    return templates.TemplateResponse(
        "driver_access.html",
        {
            "request": request,
            "router_ids": sorted(ROUTERS_CONFIG.keys()),
            "router_id": router_id,
            "phone": phone,
            "error": error,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/driver_access")
async def api_driver_access(
    request: Request,
    password: str = Form(...),
    router_id: str = Form(...),
    phone: str = Form(...),
    note: str = Form(""),
):
    def _error_redirect(msg: str) -> RedirectResponse:
        from urllib.parse import urlencode
        qs = urlencode({"router_id": router_id, "phone": phone, "error": msg})
        return RedirectResponse(url=f"/driver_access?{qs}", status_code=303)

    if not DRIVER_ACCESS_PASSWORD or not _hmac.compare_digest(password.strip(), DRIVER_ACCESS_PASSWORD):
        logger.warning("[driver_access] неверный пароль router=%s", router_id)
        return _error_redirect("Неверный пароль")

    phone_norm = _normalize_phone(phone)
    if not phone_norm:
        return _error_redirect("Некорректный номер телефона")

    if router_id not in ROUTERS_CONFIG:
        return _error_redirect("Неизвестный роутер")

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
        return _error_redirect(
            "Не удалось определить устройство. Убедитесь, что телефон водителя "
            "подключён к WiFi этого автобуса (и больше никто не подключён), и повторите."
        )

    ok = await asyncio.wait_for(
        loop.run_in_executor(MIKROTIK_EXECUTOR, grant_driver_access, mac, router_id),
        timeout=15.0,
    )
    if not ok:
        logger.error("[driver_access] grant failed mac=%s*** router=%s", mac[:8], router_id)
        return _error_redirect("Не удалось выдать доступ. Попробуйте ещё раз.")

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

    from urllib.parse import urlencode
    return RedirectResponse(
        url=f"/success?{urlencode({'mac': mac, 'router_id': router_id, 'minutes': 0, 'amount': 0, 'payment_method': 'driver'})}",
        status_code=303,
    )
