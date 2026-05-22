"""
app/routes/portal.py — Core portal routes: welcome, session_status, prepare_access,
                       prepare_and_tariffs, tariffs, and helpers.
"""

import re
import time
import asyncio
from datetime import datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..config import (
    ROUTERS_CONFIG, PREPARE_TIMEOUT_SECONDS, MIKROTIK_EXECUTOR, templates,
    QR_FALLBACK_POLL_TIMEOUT_Q_SECONDS, logger,
)
from ..db import get_db
from ..utils import (
    utf8_json_response, make_cid, _normalize_mac, _is_valid_mac,
    get_tariff_runtime_state, make_trial_signature, get_or_create_device_id,
    check_trial_used_last_24h, _set_device_cookie, _get_mac_from_cookie,
)
from ..mikrotik import set_mikrotik_ah_access, _pick_qr_mac_fallback
from ..pending import _get_busy_activation_macs

router = APIRouter()


# ── Helper: PAY_WINDOW ─────────────────────────────────────────────────────

async def _create_pay_window(mac: str, router_id: str, cid: str) -> tuple[bool, object]:
    """Открывает PAY_WINDOW на MikroTik и записывает строку в orders.
    Всегда идёт на MikroTik — без DB-кэша.
    """
    # ── Вызов MikroTik через выделенный пул потоков (20 потоков) ──────────
    loop = asyncio.get_running_loop()
    try:
        ok = await asyncio.wait_for(
            loop.run_in_executor(
                MIKROTIK_EXECUTOR,
                set_mikrotik_ah_access, mac, router_id, 3, "PAY_WINDOW",
            ),
            timeout=PREPARE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error("[PAY_WINDOW] TIMEOUT cid=%s >%ss mac=%s*** router=%s",
                     cid, PREPARE_TIMEOUT_SECONDS, mac[:8], router_id)
        return False, utf8_json_response(
            {"ok": False, "error": "Роутер отвечает слишком долго. Повторите через пару секунд."},
            status_code=504,
        )
    if not ok:
        logger.error("[PAY_WINDOW] FAIL cid=%s mac=%s*** router=%s", cid, mac[:8], router_id)
        return False, utf8_json_response(
            {"ok": False, "error": "Не удалось подготовить доступ. Повторите попытку."},
            status_code=502,
        )
    expires_at = (datetime.utcnow() + timedelta(seconds=180)).isoformat()
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO orders (mac_address, amount, status, router_id, expires_at)"
            " VALUES (?, 0, 'PAY_WINDOW', ?, ?)",
            (mac, router_id, expires_at),
        )
        conn.commit()
    finally:
        conn.close()
    return True, None


# ── Helper: tariffs response ───────────────────────────────────────────────

def _build_tariffs_response(request: Request, mac: str, router_id: str, cid: str = ""):
    tariffs, _, _, _ = get_tariff_runtime_state()
    trial_ts = str(int(time.time()))
    trial_sig = make_trial_signature(mac, router_id, trial_ts)
    device_id, _ = get_or_create_device_id(request)
    trial_used = check_trial_used_last_24h(mac, device_id)

    response = templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "mac": mac,
            "router_id": router_id,
            "cid": (cid or "-")[:24],
            "trial_ts": trial_ts,
            "trial_sig": trial_sig,
            "trial_used": "true" if trial_used else "false",
            "tariffs": tariffs,
        },
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# ── Routes ─────────────────────────────────────────────────────────────────

@router.get("/session_status")
async def session_status(mac: str, router_id: str = "astana_01"):
    if not _is_valid_mac(mac or ""):
        return utf8_json_response({"active": False, "expires_in": -1, "status": "NONE"})
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT status, expires_at FROM orders
               WHERE mac_address=? AND router_id=? AND status IN ('PAY_WINDOW','TRIAL','PAYMENT_CONFIRMED','PAID')
               ORDER BY
                 CASE status
                   WHEN 'PAID' THEN 0
                   WHEN 'PAYMENT_CONFIRMED' THEN 1
                   WHEN 'TRIAL' THEN 2
                   WHEN 'PAY_WINDOW' THEN 3
                   ELSE 3
                 END,
                 created_at DESC
               LIMIT 1""",
            (mac, router_id),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return utf8_json_response({"active": False, "expires_in": -1, "status": "NONE"})
    status, expires_at_str = row
    if status == "PAYMENT_CONFIRMED":
        return utf8_json_response({"active": False, "expires_in": -1, "status": status, "pending_activation": True})
    if not expires_at_str:
        return utf8_json_response({"active": True, "expires_in": -1, "status": status})
    try:
        expires_at = datetime.fromisoformat(expires_at_str)
        expires_in = int((expires_at - datetime.utcnow()).total_seconds())
        return utf8_json_response({"active": expires_in > 0, "expires_in": expires_in, "status": status})
    except Exception:
        return utf8_json_response({"active": False, "expires_in": -1, "status": status})


@router.post("/api/prepare_access")
async def prepare_access(request: Request):
    t_start = time.monotonic()
    data = await request.json()
    mac = data.get("mac", "")
    router_id = data.get("router_id", "astana_01")
    cid = (data.get("cid") or "-")[:24]
    logger.info(f"[prepare_access] START cid={cid} mac={mac[:8]}*** router={router_id}")

    if not _is_valid_mac(mac or ""):
        return utf8_json_response({"ok": False, "error": "Некорректный MAC"}, status_code=400)
    if router_id not in ROUTERS_CONFIG:
        return utf8_json_response({"ok": False, "error": "Неизвестный роутер"}, status_code=400)

    ok, err = await _create_pay_window(mac, router_id, cid)
    if not ok:
        return err
    logger.info("[prepare_access] DONE cid=%s in %.0fms ✓ mac=%s*** router=%s",
                cid, (time.monotonic() - t_start) * 1000, mac[:8], router_id)
    return utf8_json_response({"ok": True})


@router.get("/", response_class=HTMLResponse)
async def welcome(request: Request, mac: str = "00:00:00:00:00:00", router_id: str = "astana_01", cid: str = ""):
    cid = (cid or make_cid())[:24]
    mac_norm = _normalize_mac(mac)
    logger.info(f"[welcome] cid={cid} mac={mac_norm[:8]}*** router={router_id}")

    if not _is_valid_mac(mac_norm) and router_id in ROUTERS_CONFIG:
        busy_macs = _get_busy_activation_macs(router_id)
        fallback_reason = "not_tried"
        loop = asyncio.get_running_loop()
        try:
            fallback_mac, fallback_reason = await asyncio.wait_for(
                loop.run_in_executor(MIKROTIK_EXECUTOR, _pick_qr_mac_fallback, router_id, busy_macs),
                timeout=QR_FALLBACK_POLL_TIMEOUT_Q_SECONDS,
            )
        except asyncio.TimeoutError:
            fallback_mac, fallback_reason = None, "router_timeout"
        except Exception:
            fallback_mac, fallback_reason = None, "router_error"

        if fallback_mac:
            logger.info("[welcome] fallback hit mac=%s router=%s reason=%s cid=%s",
                        fallback_mac[:8] + '***', router_id, fallback_reason, cid)
            return RedirectResponse(
                url=f"/?{urlencode({'mac': fallback_mac, 'router_id': router_id, 'cid': cid})}",
                status_code=303,
            )

        logger.info("[welcome] invalid mac -> qr fallback router=%s reason=%s cid=%s raw_mac=%s",
                    router_id, fallback_reason, cid, mac_norm[:32])
        return RedirectResponse(
            url=f"/q?{urlencode({'router_id': router_id, 'cid': cid})}",
            status_code=303,
        )

    response = templates.TemplateResponse("welcome.html", {"request": request, "mac": mac, "router_id": router_id, "cid": cid})
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    if _is_valid_mac(mac_norm) and router_id in ROUTERS_CONFIG:
        _set_device_cookie(response, mac_norm, router_id)
        logger.info("[welcome] cookie set mac=%s router=%s cid=%s", mac_norm[:8] + '***', router_id, cid)
    return response


@router.get("/prepare_and_tariffs", response_class=HTMLResponse)
async def prepare_and_tariffs(request: Request, mac: str, router_id: str = "astana_01", cid: str = ""):
    cid = (cid or make_cid())[:24]
    logger.info(f"[prepare_and_tariffs] START cid={cid} mac={mac[:8]}*** router={router_id}")

    if not _is_valid_mac(mac or ""):
        return templates.TemplateResponse("welcome.html", {
            "request": request, "mac": mac, "router_id": router_id, "cid": cid, "error": "Некорректный MAC",
        })
    if router_id not in ROUTERS_CONFIG:
        return templates.TemplateResponse("welcome.html", {
            "request": request, "mac": mac, "router_id": router_id, "cid": cid, "error": "Неизвестный роутер",
        })

    # PAY_WINDOW запускаем полностью в фоне — НЕ ждём ответа.
    # iOS CNA видит смену доступа (выданный интернет) ВО ВРЕМЯ навигации и зависает на 40+ сек.
    # Пока пользователь читает тарифы (~5-10с), фоновый поток создаёт биндинг (~200ms).
    # /start_payment создаёт биндинг ещё раз перед самой оплатой как страховку.
    asyncio.ensure_future(_create_pay_window(mac, router_id, cid))

    user_agent = (request.headers.get("user-agent") or "")
    is_android = bool(re.search(r"android", user_agent, re.IGNORECASE))
    tariff_url = f"/tariffs?{urlencode({'mac': mac, 'router_id': router_id, 'cid': cid})}"

    if is_android:
        logger.info(f"[prepare_and_tariffs] android bridge cid={cid} mac={mac[:8]}***")
        response = templates.TemplateResponse(
            "android_bridge.html",
            {"request": request, "mac": mac, "router_id": router_id, "cid": cid},
        )
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["Refresh"] = f"0; url={tariff_url}"
        return response

    logger.info(f"[prepare_and_tariffs] redirect cid={cid} -> /tariffs (PAY_WINDOW в фоне) mac={mac[:8]}***")
    return RedirectResponse(url=tariff_url, status_code=303)


@router.get("/tariffs", response_class=HTMLResponse)
async def tariffs(request: Request, mac: str = "00:00:00:00:00:00", router_id: str = "astana_01", cid: str = ""):
    cid = (cid or make_cid())[:24]
    logger.info(f"[tariffs] cid={cid} mac={mac[:8]}*** router={router_id}")
    return _build_tariffs_response(request, mac, router_id, cid)
