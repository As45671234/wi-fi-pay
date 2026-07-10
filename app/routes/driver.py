"""
app/routes/driver.py — Driver whitelist provisioning (замена grant_permanent_access.sh).

Flow (два шага, пароль отдельно от формы):
  GET  /driver_access               → шаг 1: только пароль
  POST /driver_access               → проверка пароля → шаг 2: роутер (id) + телефон + комментарий
  POST /api/driver_access           → повторная проверка пароля, лимит 5 телефона на роутер,
                                       автоопределение MAC на роутере (с ретраями), выдача
                                       бессрочного доступа, запись в driver_phones.

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

# Автобусы часто на мобильном интернете — единственная попытка с коротким
# socket_timeout (как у QR-flow, заточенного под частый клиентский polling)
# слишком часто фейлится. Здесь — разовое админ-действие, поэтому пробуем
# несколько раз с большим таймаутом и большим допуском по idle-time (водитель
# не обязан активно листать страницы, пока админ заполняет форму).
_MAC_DETECT_ATTEMPTS = 4
_MAC_DETECT_SOCKET_TIMEOUT_SECONDS = 3.5
_MAC_DETECT_RETRY_DELAY_SECONDS = 1.5
_MAC_DETECT_MAX_IDLE_SECONDS = 180
_MAX_DRIVERS_PER_ROUTER = 5


async def _detect_driver_mac(router_id: str, busy_macs: set[str]) -> tuple[str | None, str]:
    loop = asyncio.get_running_loop()
    reason = "not_tried"
    for attempt in range(1, _MAC_DETECT_ATTEMPTS + 1):
        try:
            mac, reason = await asyncio.wait_for(
                loop.run_in_executor(
                    MIKROTIK_EXECUTOR, _pick_qr_mac_fallback, router_id, busy_macs,
                    _MAC_DETECT_MAX_IDLE_SECONDS, _MAC_DETECT_SOCKET_TIMEOUT_SECONDS,
                ),
                timeout=_MAC_DETECT_SOCKET_TIMEOUT_SECONDS + 3.0,
            )
        except asyncio.TimeoutError:
            mac, reason = None, "router_timeout"
        except Exception:
            mac, reason = None, "router_error"

        if mac:
            return mac, reason
        if reason == "router_unknown":
            break
        logger.info("[driver_access] попытка %d/%d неудачна router=%s reason=%s",
                    attempt, _MAC_DETECT_ATTEMPTS, router_id, reason)
        if attempt < _MAC_DETECT_ATTEMPTS:
            await asyncio.sleep(_MAC_DETECT_RETRY_DELAY_SECONDS)
    return None, reason


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


def _render_form(request: Request, password: str, router_id: str = "", phone: str = "", note: str = "", error: str = "", info: str = ""):
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
            "info": info,
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
    mac, reason = await _detect_driver_mac(router_id, busy_macs)

    granted = False
    if mac:
        try:
            granted = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(MIKROTIK_EXECUTOR, grant_driver_access, mac, router_id),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            granted = False
        if not granted:
            logger.error("[driver_access] grant failed mac=%s*** router=%s", mac[:8], router_id)

    # Номер сохраняем в белый список ВСЕГДА, даже если MAC не определился или
    # MikroTik временно недоступен — иначе номер "терялся" и его приходилось
    # вводить заново. mac_address может остаться NULL: как только водитель сам
    # зайдёт на /restore_access со своим телефоном, система привяжет реальный
    # MAC его устройства (см. app/routes/restore.py::_get_driver_binding).
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

    if granted:
        logger.info("[driver_access] SUCCESS phone=%s*** mac=%s*** router=%s", phone_norm[:7], mac[:8], router_id)
        return RedirectResponse(
            url=f"/success?{urlencode({'mac': mac, 'router_id': router_id, 'minutes': 0, 'amount': 0, 'payment_method': 'driver'})}",
            status_code=303,
        )

    logger.info("[driver_access] SAVED pending grant phone=%s*** router=%s mac_found=%s reason=%s",
                phone_norm[:7], router_id, bool(mac), reason)
    if mac:
        info = ("Номер сохранён, но роутер не подтвердил доступ (временная проблема сети). "
                "Попробуйте ещё раз, либо водитель сам восстановит доступ на wifi-pay.kz "
                "через «Уже оплачивали? Восстановить доступ» тем же номером.")
    elif reason == "ambiguous":
        info = ("Номер сохранён в списке водителей роутера {rid}. Устройство пока не определено — "
                "в сети сейчас несколько новых устройств. Попробуйте ещё раз, либо водитель сам "
                "восстановит доступ на wifi-pay.kz через «Уже оплачивали? Восстановить доступ».").format(rid=router_id)
    else:
        info = ("Номер сохранён в списке водителей роутера {rid}. Устройство пока не определено. "
                "Доступ применится автоматически, как только водитель зайдёт на wifi-pay.kz "
                "и нажмёт «Уже оплачивали? Восстановить доступ», введя этот номер.").format(rid=router_id)
    return _render_form(request, password, router_id, info=info)
