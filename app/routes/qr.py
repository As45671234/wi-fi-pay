"""
app/routes/qr.py — QR-code flow routes: /api/qr/sign, /q, /q/auto, /q/select.
"""

import asyncio
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..config import (
    ROUTERS_CONFIG, BASE_URL, templates,
    QR_FALLBACK_POLL_TIMEOUT_Q_SECONDS, QR_FALLBACK_POLL_TIMEOUT_AUTO_SECONDS,
    QR_CLIENT_AUTO_RETRY_MS, QR_CLIENT_FIRST_RETRY_MS,
    DEVICE_COOKIE_NAME, logger,
)
from ..utils import (
    utf8_json_response, make_cid, _normalize_mac, _is_valid_mac,
    make_router_qr_signature, is_valid_router_qr_signature,
    _get_mac_from_cookie,
)
from ..mikrotik import _pick_qr_mac_fallback
from ..pending import _get_busy_activation_macs

router = APIRouter()


@router.get("/api/qr/sign")
async def sign_qr_router_link(router_id: str):
    if router_id not in ROUTERS_CONFIG:
        return utf8_json_response({"error": "Неизвестный роутер"}, status_code=400)
    ts = str(int(time.time()))
    sig = make_router_qr_signature(router_id, ts)
    return utf8_json_response({
        "ok": True,
        "router_id": router_id,
        "ts": ts,
        "sig": sig,
        "url": f"{BASE_URL}/q?r={router_id}&ts={ts}&sig={sig}",
    })


@router.get("/q", response_class=HTMLResponse)
async def qr_entry(
    request: Request,
    router_id: str = "",
    r: str = "",
    ts: str = "",
    sig: str = "",
    cid: str = "",
):
    cid = (cid or make_cid())[:24]
    router_id = (r or router_id or "").strip()

    if not router_id:
        return utf8_json_response({"error": "router_id required"}, status_code=400)
    if router_id not in ROUTERS_CONFIG:
        return utf8_json_response({"error": "Неизвестный роутер"}, status_code=400)

    signed_mode = bool(sig or ts)
    if signed_mode and not is_valid_router_qr_signature(router_id, ts, sig):
        return utf8_json_response({"error": "Невалидная или просроченная QR-подпись"}, status_code=403)

    cookie_data = _get_mac_from_cookie(request)
    if cookie_data:
        mac_from_cookie, router_from_cookie = cookie_data
        if router_from_cookie == router_id:
            logger.info("[QR] cookie hit mac=%s router=%s cid=%s", mac_from_cookie[:8] + '***', router_id, cid)
            return RedirectResponse(
                url=f"/?{urlencode({'mac': mac_from_cookie, 'router_id': router_id, 'cid': cid})}",
                status_code=303,
            )
        logger.info("[QR] cookie mismatch (cookie_router=%s qr_router=%s) cid=%s",
                    router_from_cookie, router_id, cid)

    if cookie_data and cookie_data[1] != router_id:
        fallback_reason = "cookie_router_mismatch"
        logger.info("[QR] no mac state=wrong_router fallback=%s router=%s cid=%s", fallback_reason, router_id, cid)
        resp = templates.TemplateResponse(
            "qr_entry.html",
            {
                "request": request, "router_id": router_id, "cid": cid,
                "signed_mode": "true" if signed_mode else "false",
                "detection_state": "wrong_router", "fallback_reason": fallback_reason,
                "ts": ts, "sig": sig,
                "auto_retry_ms": QR_CLIENT_AUTO_RETRY_MS,
                "first_retry_ms": QR_CLIENT_FIRST_RETRY_MS,
            },
        )
        resp.delete_cookie(DEVICE_COOKIE_NAME)
        return resp

    busy_macs = _get_busy_activation_macs(router_id)
    fallback_reason = "not_tried"
    try:
        fallback_mac, fallback_reason = await asyncio.wait_for(
            asyncio.to_thread(_pick_qr_mac_fallback, router_id, busy_macs),
            timeout=QR_FALLBACK_POLL_TIMEOUT_Q_SECONDS,
        )
    except asyncio.TimeoutError:
        fallback_mac, fallback_reason = None, "router_timeout"
    except Exception:
        fallback_mac, fallback_reason = None, "router_error"

    if fallback_mac:
        logger.info("[QR] fallback hit mac=%s router=%s reason=%s cid=%s",
                    fallback_mac[:8] + '***', router_id, fallback_reason, cid)
        return RedirectResponse(
            url=f"/?{urlencode({'mac': fallback_mac, 'router_id': router_id, 'cid': cid})}",
            status_code=303,
        )

    has_cookie = cookie_data is not None
    if has_cookie:
        detection_state = "wrong_router"
    elif fallback_reason in ("no_clients",):
        detection_state = "no_clients"
    elif fallback_reason in ("ambiguous",):
        detection_state = "ambiguous"
    else:
        detection_state = "no_cookie"

    logger.info("[QR] no mac state=%s fallback=%s router=%s cid=%s", detection_state, fallback_reason, router_id, cid)

    resp = templates.TemplateResponse(
        "qr_entry.html",
        {
            "request": request, "router_id": router_id, "cid": cid,
            "signed_mode": "true" if signed_mode else "false",
            "detection_state": detection_state, "fallback_reason": fallback_reason,
            "ts": ts, "sig": sig,
            "auto_retry_ms": QR_CLIENT_AUTO_RETRY_MS,
            "first_retry_ms": QR_CLIENT_FIRST_RETRY_MS,
        },
    )
    if has_cookie:
        resp.delete_cookie(DEVICE_COOKIE_NAME)
    return resp


@router.get("/q/auto")
async def qr_auto_pick(request: Request, router_id: str, cid: str = "", ts: str = "", sig: str = ""):
    cid = (cid or make_cid())[:24]
    if router_id not in ROUTERS_CONFIG:
        return utf8_json_response({"error": "Неизвестный роутер"}, status_code=400)
    if (sig or ts) and not is_valid_router_qr_signature(router_id, ts, sig):
        return utf8_json_response({"error": "Невалидная или просроченная QR-подпись"}, status_code=403)

    cookie_data = _get_mac_from_cookie(request)
    if cookie_data:
        mac_from_cookie, router_from_cookie = cookie_data
        if router_from_cookie == router_id:
            logger.info("[QR/auto] cookie hit mac=%s router=%s cid=%s", mac_from_cookie[:8] + '***', router_id, cid)
            return RedirectResponse(
                url=f"/?{urlencode({'mac': mac_from_cookie, 'router_id': router_id, 'cid': cid})}",
                status_code=303,
            )

    busy_macs = _get_busy_activation_macs(router_id)
    try:
        fallback_mac, fallback_reason = await asyncio.wait_for(
            asyncio.to_thread(_pick_qr_mac_fallback, router_id, busy_macs),
            timeout=QR_FALLBACK_POLL_TIMEOUT_AUTO_SECONDS,
        )
    except asyncio.TimeoutError:
        fallback_mac, fallback_reason = None, "router_timeout"
    except Exception:
        fallback_mac, fallback_reason = None, "router_error"

    if fallback_mac:
        logger.info("[QR/auto] fallback hit mac=%s router=%s reason=%s cid=%s",
                    fallback_mac[:8] + '***', router_id, fallback_reason, cid)
        return RedirectResponse(
            url=f"/?{urlencode({'mac': fallback_mac, 'router_id': router_id, 'cid': cid})}",
            status_code=303,
        )

    return RedirectResponse(
        url=f"/q?{urlencode({'router_id': router_id, 'cid': cid, 'ts': ts, 'sig': sig})}",
        status_code=303,
    )


@router.get("/q/select")
async def qr_select_mac(mac: str, router_id: str, cid: str = ""):
    cid = (cid or make_cid())[:24]
    mac_norm = _normalize_mac(mac)
    if not _is_valid_mac(mac_norm):
        return utf8_json_response({"error": "Некорректный MAC"}, status_code=400)
    if router_id not in ROUTERS_CONFIG:
        return utf8_json_response({"error": "Неизвестный роутер"}, status_code=400)

    if _normalize_mac(mac_norm) in _get_busy_activation_macs(router_id):
        return utf8_json_response(
            {"error": "Для этого устройства уже выполняется активация. Подождите 10-20 секунд."},
            status_code=409,
        )

    tariff_url = f"/tariffs?{urlencode({'mac': mac_norm, 'router_id': router_id, 'cid': cid})}"
    return RedirectResponse(url=tariff_url, status_code=303)
