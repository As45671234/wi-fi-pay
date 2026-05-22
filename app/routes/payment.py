"""
app/routes/payment.py — FreedomPay payment routes: choose_payment, start_payment,
                        payment_result, success, get_free_trial, activate_welcome.
"""

import re
import time
import sqlite3
import asyncio
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from ..config import (
    ROUTERS_CONFIG, MERCHANT_ID, SECRET_KEY, BASE_URL,
    KASPI_ENABLED, templates, logger,
)
from ..db import get_db
from ..utils import utf8_json_response, _normalize_mac, _is_valid_mac, get_tariff_runtime_state
from ..payments import get_signature, decode_nested_url_value, build_payment_url
from ..pending import _enqueue_pending_activation, _drain_pending_activations
from .portal import _create_pay_window

router = APIRouter()


@router.get("/choose_payment", response_class=HTMLResponse)
async def choose_payment(
    request: Request,
    amount: int,
    mac: str,
    router_id: str = "astana_01",
    cid: str = "",
):
    cid = (cid or "-")[:24]
    logger.info(f"[choose_payment] cid={cid} amount={amount} mac={mac[:8]}*** router={router_id}")

    _, _, amount_to_title, allowed_amounts = get_tariff_runtime_state()
    if amount not in allowed_amounts:
        return utf8_json_response({"error": "Некорректная сумма"}, status_code=400)
    if not _is_valid_mac(mac or ""):
        return utf8_json_response({"error": "Некорректный MAC-адрес"}, status_code=400)
    if router_id not in ROUTERS_CONFIG:
        logger.error(f"[choose_payment] Неизвестный router_id: {router_id}")
        return utf8_json_response({"error": "Неизвестный роутер"}, status_code=400)

    tariff_name = amount_to_title.get(amount, "")
    response = templates.TemplateResponse(
        "choose_payment.html",
        {
            "request": request, "amount": amount, "mac": mac,
            "router_id": router_id, "cid": cid,
            "tariff_name": tariff_name, "kaspi_enabled": KASPI_ENABLED,
        },
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@router.get("/start_payment")
async def start_payment(request: Request, amount: int, mac: str, router_id: str = "astana_01", cid: str = ""):
    cid = (cid or "-")[:24]
    logger.info(f"[start_payment] START cid={cid} amount={amount} mac={mac[:8]}*** router={router_id}")
    if not MERCHANT_ID or not SECRET_KEY:
        logger.error("[start_payment] FreedomPay env is not configured")
        return utf8_json_response({"error": "Платежный шлюз временно недоступен"}, status_code=503)
    _, _, _, allowed_amounts = get_tariff_runtime_state()
    if amount not in allowed_amounts:
        return utf8_json_response({"error": "Некорректная сумма"}, status_code=400)
    if not _is_valid_mac(mac or ""):
        return utf8_json_response({"error": "Некорректный MAC-адрес"}, status_code=400)
    if router_id not in ROUTERS_CONFIG:
        logger.error(f"[start_payment] Неизвестный router_id: {router_id}")
        return utf8_json_response({"error": "Неизвестный роутер"}, status_code=400)

    # Синхронно гарантируем PAY_WINDOW перед редиректом на FreedomPay.
    # force=True — всегда идём на MikroTik, не используем DB-кэш.
    ok, _err = await _create_pay_window(mac, router_id, cid)
    if not ok:
        logger.warning(f"[start_payment] PAY_WINDOW FAIL (продолжаем) cid={cid} mac={mac[:8]}*** router={router_id}")
    else:
        logger.info(f"[start_payment] PAY_WINDOW OK cid={cid} mac={mac[:8]}*** router={router_id}")

    payment_order_id = str(int(time.time() * 1000))
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO orders (mac_address, amount, status, router_id, payment_order_id) VALUES (?, ?, 'PAYMENT_INITIATED', ?, ?)",
            (mac, amount, router_id, payment_order_id),
        )
        conn.commit()
    finally:
        conn.close()

    payment_url = build_payment_url(amount, mac, router_id, payment_order_id, cid)
    logger.info(f"[start_payment] REDIRECT cid={cid} order={payment_order_id} mac={mac[:8]}*** router={router_id}")
    return RedirectResponse(url=payment_url, status_code=302)


@router.get("/activate_welcome")
async def activate_welcome(request: Request, mac: str, router_id: str = "astana_01"):
    if not _is_valid_mac(mac or ""):
        logger.warning(f"[activate_welcome] Некорректный MAC: {mac}")
        return utf8_json_response({"error": "Некорректный MAC"}, status_code=400)
    if router_id not in ROUTERS_CONFIG:
        logger.error(f"[activate_welcome] Неизвестный router_id: {router_id}")
        return utf8_json_response({"error": "Неизвестный роутер"}, status_code=400)

    logger.warning(f"[activate_welcome] legacy call mac={mac[:8]}*** router={router_id} — redirecting to welcome flow")
    return RedirectResponse(
        url=f"/?{urlencode({'mac': mac, 'router_id': router_id})}",
        status_code=302,
    )


@router.post("/get_free_trial")
async def get_free_trial(
    request: Request,
    mac: str = Form(...),
    router_id: str = Form(...),
    trial_ts: str = Form(""),
    trial_sig: str = Form(""),
    cid: str = Form(""),
):
    cid = (cid or "-")[:24]
    logger.info(f"[get_free_trial] DISABLED cid={cid} mac={mac[:8]}*** router={router_id}")
    return utf8_json_response({"error": "Бесплатный доступ временно отключен"}, status_code=403)


@router.post("/payment_result")
async def payment_result(request: Request):
    try:
        form_data = await request.form()
        params = dict(form_data)
        cid = (params.get('cid') or params.get('pg_param3') or '-')[:24]

        logger.info(f"[payment_result] Callback cid={cid} order_id={params.get('pg_order_id')}, amount={params.get('pg_amount')} ₸")

        if params.get('pg_sig') != get_signature("payment_result", params, SECRET_KEY):
            logger.error(f"[payment_result] ❌ НЕВАЛИДНАЯ ПОДПИСЬ от FreedomPay")
            return Response(content="Invalid signature", status_code=400)

        if params.get('pg_result') != '1':
            logger.info(f"[payment_result] ⚠️ Платеж не прошел (pg_result={params.get('pg_result')})")
            return Response(content="Payment not successful", status_code=400)

        payment_order_id = (params.get('pg_order_id') or '').strip()
        mac = params.get('pg_param1')
        router_id = params.get('pg_param2')
        amount = int(float(params.get('pg_amount', 0)))

        logger.info(f"[payment_result] ✓ Платеж успешен: {amount} ₸, MAC: {mac}, router: {router_id}")

        if not mac:
            description = params.get('pg_description', '')
            mac_match = re.search(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})", description)
            if mac_match:
                mac = mac_match.group(1)
                logger.info(f"[payment_result] MAC восстановлен из описания: {mac[:8]}***")

        if not mac or not router_id:
            conn = get_db()
            try:
                row = None
                if payment_order_id:
                    row = conn.execute(
                        "SELECT mac_address, router_id, payment_order_id FROM orders WHERE payment_order_id = ? ORDER BY id DESC LIMIT 1",
                        (payment_order_id,),
                    ).fetchone()
                elif mac:
                    row = conn.execute(
                        "SELECT mac_address, router_id, payment_order_id FROM orders WHERE mac_address = ? AND status IN ('PAYMENT_INITIATED', 'PAY_WINDOW') ORDER BY id DESC LIMIT 1",
                        (mac,),
                    ).fetchone()
            finally:
                conn.close()
            if row:
                mac = mac or row[0]
                router_id = router_id or row[1]
                payment_order_id = payment_order_id or (row[2] or '')

        router_id = router_id or 'astana_01'
        _, amount_to_minutes, _, _ = get_tariff_runtime_state()
        minutes = amount_to_minutes.get(amount, 60)
        logger.info(f"[payment_result] queue activation on {minutes} мин для {mac[:8]}*** ({router_id})")

        if not mac:
            logger.error("[payment_result] ❌ ОШИБКА: MAC не найден")
            return Response(content="MAC not found", status_code=500)

        try:
            pending_id = await asyncio.to_thread(
                _enqueue_pending_activation, router_id, mac, amount, minutes, payment_order_id,
            )
            logger.info("[payment_result] pending activation id=%s queued", pending_id)
        except Exception as e:
            logger.error("[payment_result] ❌ ОШИБКА постановки в очередь активации: %s", str(e)[:200])
            return Response(content="Activation queue failed", status_code=500)

        await _drain_pending_activations(limit=2)

        conn = get_db()
        try:
            updated = 0
            if payment_order_id:
                updated = conn.execute(
                    "UPDATE orders SET status = 'PAYMENT_CONFIRMED', amount = ?, mac_address = ?, router_id = ? WHERE payment_order_id = ?",
                    (amount, mac, router_id, payment_order_id),
                ).rowcount
            elif mac:
                updated = conn.execute(
                    "UPDATE orders SET status = 'PAYMENT_CONFIRMED', amount = ?, router_id = ? WHERE id = (SELECT id FROM orders WHERE mac_address = ? AND status IN ('PAYMENT_INITIATED', 'PAY_WINDOW', 'PAYMENT_CONFIRMED') ORDER BY id DESC LIMIT 1)",
                    (amount, router_id, mac),
                ).rowcount

            if not updated:
                conn.execute(
                    "INSERT INTO orders (mac_address, amount, status, router_id, payment_order_id) VALUES (?, ?, 'PAYMENT_CONFIRMED', ?, ?)",
                    (mac, amount, router_id, payment_order_id),
                )
            conn.commit()
            logger.info(f"[payment_result] ✓ УСПЕХ: {amount} ₸ подтверждено для {mac[:8]}***, активация через очередь")
            logger.info(f"[payment_result] 🔍 Для диагностики: http://wifi-pay.kz/debug?mac={mac}&router_id={router_id}")
        finally:
            conn.close()

        return Response(content="OK", status_code=200)

    except Exception as e:
        logger.error(f"[payment_result] ❌ ОШИБКА обработки платежа: {str(e)[:200]}")
        return Response(content="Internal error", status_code=500)


@router.get("/success", response_class=HTMLResponse)
async def success(
    request: Request,
    mac: str = "00:00:00:00:00:00",
    router_id: str = "astana_01",
    minutes: int = 60,
    amount: int = 0,
    payment_method: str = "card",
    contract_number: str = "",
):
    mac = decode_nested_url_value(mac)
    router_id = decode_nested_url_value(router_id)
    contract_number = decode_nested_url_value(contract_number).strip().upper()
    _, _, amount_to_title, _ = get_tariff_runtime_state()
    tariff_name = amount_to_title.get(amount, "")

    kaspi_status = ""
    kaspi_is_activated = False
    if payment_method == "kaspi" and contract_number:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT kaspi_status, is_activated FROM kaspi_orders WHERE contract_number = ?",
                (contract_number,),
            ).fetchone()
            if row:
                kaspi_status = (row["kaspi_status"] or "")
                kaspi_is_activated = bool(row["is_activated"])
        finally:
            conn.close()

    return templates.TemplateResponse("success.html", {
        "request": request, "mac": mac, "router_id": router_id,
        "minutes": minutes, "amount": amount, "tariff_name": tariff_name,
        "payment_method": payment_method, "contract_number": contract_number,
        "kaspi_status": kaspi_status,
        "kaspi_is_activated": "true" if kaspi_is_activated else "false",
    })
