"""
app/routes/kaspi.py — All /api/kaspi/* routes and /docs/kaspi-check-pay.
"""

import sqlite3
import asyncio
import time

from fastapi import APIRouter, Request

from ..config import ROUTERS_CONFIG, KASPI_ENABLED, BASE_URL, logger
from ..db import get_db
from ..utils import utf8_json_response, _normalize_mac, _is_valid_mac, get_tariff_runtime_state
from ..kaspi_service import (
    KaspiCreateOrderRequest, KaspiStatusResponse,
    KASPI_CHECKPAY_RESULT_OK, KASPI_CHECKPAY_RESULT_INVALID_REQUEST,
    KASPI_CHECKPAY_RESULT_CONTRACT_NOT_FOUND, KASPI_CHECKPAY_RESULT_AMOUNT_MISMATCH,
    KASPI_CHECKPAY_RESULT_ALREADY_PAID, KASPI_CHECKPAY_RESULT_TX_CONFLICT,
    KASPI_CHECKPAY_RESULT_INTERNAL_ERROR,
    _pick_value, _pick_amount_value, _kaspi_request_id, _kaspi_response,
    _normalize_contract_number, _has_valid_checkpay_auth,
    _fetch_kaspi_order_by_contract, _fetch_contract_by_transaction_id,
    _upsert_kaspi_remote_state, _process_kaspi_paid,
    make_contract_number, kaspi_sync_once,
)
from ..config import KASPI_PAID_STATUSES

router = APIRouter()


async def _parse_kaspi_request_data(request: Request) -> dict:
    if request.method == "GET":
        return dict(request.query_params)
    try:
        data = await request.json()
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    try:
        form = await request.form()
        return dict(form)
    except Exception:
        pass
    return {}


@router.post("/api/kaspi/create_order")
async def create_kaspi_order(payload: KaspiCreateOrderRequest):
    if not KASPI_ENABLED:
        return utf8_json_response({"error": "Kaspi режим отключен"}, status_code=403)

    amount = int(payload.amount)
    mac = _normalize_mac(payload.mac)
    router_id = payload.router_id
    cid = (payload.cid or "-")[:24]

    _, amount_to_minutes, amount_to_title, allowed_amounts = get_tariff_runtime_state()
    if amount not in allowed_amounts:
        return utf8_json_response({"error": "Некорректная сумма"}, status_code=400)
    if not _is_valid_mac(mac):
        return utf8_json_response({"error": "Некорректный MAC-адрес"}, status_code=400)
    if router_id not in ROUTERS_CONFIG:
        return utf8_json_response({"error": "Неизвестный роутер"}, status_code=400)

    contract_number = make_contract_number(mac)
    local_order_id = f"kaspi_{int(time.time() * 1000)}"
    minutes = int(amount_to_minutes.get(amount, 60))
    from ..utils import _normalize_phone
    phone_norm = _normalize_phone(payload.phone) or None

    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO kaspi_orders (
                local_order_id, contract_number, external_order_ref,
                mac_address, router_id, amount, minutes, kaspi_status, is_activated,
                phone, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(contract_number) DO UPDATE SET
                local_order_id = excluded.local_order_id,
                external_order_ref = excluded.external_order_ref,
                router_id = excluded.router_id,
                amount = excluded.amount,
                minutes = excluded.minutes,
                kaspi_status = 'CREATED',
                is_activated = 0,
                kaspi_order_id = NULL,
                paid_at = NULL,
                phone = COALESCE(excluded.phone, phone),
                activation_lock = 0,
                activation_attempts = 0,
                last_activation_attempt_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            """,
            (local_order_id, contract_number, contract_number, mac, router_id, amount, minutes, "CREATED", phone_norm),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info("[KASPI] local order created cid=%s contract=%s mac=%s router=%s amount=%s",
                cid, contract_number, mac[:8] + "***", router_id, amount)

    return utf8_json_response({
        "ok": True,
        "local_order_id": local_order_id,
        "contract_number": contract_number,
        "amount": amount,
        "minutes": minutes,
        "tariff_name": amount_to_title.get(amount, ""),
        "status_url": f"/api/kaspi/order_status?contract_number={contract_number}",
        "success_url": f"/success?mac={mac}&router_id={router_id}&minutes={minutes}&amount={amount}&payment_method=kaspi&contract_number={contract_number}&cid={cid}",
    })


@router.get("/api/kaspi/order_status")
async def kaspi_order_status(contract_number: str):
    contract = (contract_number or "").strip().upper()
    if not contract:
        return utf8_json_response({"error": "contract_number required"}, status_code=400)

    conn = get_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT contract_number, kaspi_status, is_activated, paid_at, activated_at, amount, minutes
            FROM kaspi_orders
            WHERE contract_number = ?
            """,
            (contract,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return utf8_json_response({"error": "order not found"}, status_code=404)

    return utf8_json_response(
        KaspiStatusResponse(
            contract_number=row["contract_number"],
            kaspi_status=(row["kaspi_status"] or ""),
            is_activated=bool(row["is_activated"]),
            paid_at=row["paid_at"],
            activated_at=row["activated_at"],
            amount=int(row["amount"] or 0),
            minutes=int(row["minutes"] or 0),
        ).dict()
    )


@router.get("/docs/kaspi-check-pay")
async def kaspi_check_pay_docs():
    return utf8_json_response({
        "service": "WiFi Pay",
        "base_url": BASE_URL,
        "protocol": "HTTPS",
        "method": "POST",
        "content_type": "application/json; charset=UTF-8",
        "check": {
            "url": f"{BASE_URL}/api/kaspi/check",
            "request_example": {"request_id": "CHK-0001", "contract_number": "A13AABBCCDDEEFF1234567890ABCD", "amount": 1500},
            "response_success_example": {"request_id": "CHK-0001", "result": 0, "message": "OK", "can_pay": True,
                                         "contract_number": "A13AABBCCDDEEFF1234567890ABCD", "amount": 1500, "currency": "KZT"},
            "response_error_example": {"request_id": "CHK-0002", "result": 5, "message": "Contract not found", "can_pay": False},
        },
        "pay": {
            "url": f"{BASE_URL}/api/kaspi/pay",
            "request_example": {"request_id": "PAY-0001", "transaction_id": "KASPI-TXN-987654321",
                                 "payment_datetime": "2026-04-24T15:30:00+05:00",
                                 "contract_number": "A13AABBCCDDEEFF1234567890ABCD", "amount": 1500},
            "response_success_example": {"request_id": "PAY-0001", "result": 0, "message": "Payment accepted",
                                         "transaction_id": "KASPI-TXN-987654321", "status": "ACCEPTED"},
            "response_duplicate_example": {"request_id": "PAY-0001-R", "result": 0, "message": "Already processed",
                                           "transaction_id": "KASPI-TXN-987654321", "status": "DUPLICATE_OK"},
        },
        "result_codes": {"0": "OK", "5": "Contract not found", "6": "Amount mismatch", "7": "Invalid request",
                         "8": "Already paid", "9": "Transaction conflict", "96": "Internal error"},
    })


@router.api_route("/api/kaspi/check", methods=["GET", "POST"])
async def kaspi_check(request: Request):
    if not _has_valid_checkpay_auth(request):
        return utf8_json_response({"result": 401, "message": "Unauthorized"}, status_code=401)

    data = await _parse_kaspi_request_data(request)
    if not data:
        return _kaspi_response("-", KASPI_CHECKPAY_RESULT_INVALID_REQUEST, "Invalid request")

    request_id = _kaspi_request_id(data)
    contract_number = _normalize_contract_number(
        _pick_value(data, "contract_number", "contractNumber", "account", "account_id", "order_id", "orderId")
    )
    amount = _pick_amount_value(data, "amount", "sum", "payment_amount", "paymentAmount")
    phone = _pick_value(data, "phone", "phone_number", "phoneNumber", "subscriber")[:32] or None

    if not contract_number:
        return _kaspi_response(request_id, KASPI_CHECKPAY_RESULT_INVALID_REQUEST, "contract_number is required", {"can_pay": False})

    row = _fetch_kaspi_order_by_contract(contract_number)
    if not row:
        return _kaspi_response(request_id, KASPI_CHECKPAY_RESULT_CONTRACT_NOT_FOUND, "Contract not found", {"can_pay": False})

    expected_amount = int(row["amount"] or 0)
    if amount is not None and amount != expected_amount:
        return _kaspi_response(request_id, KASPI_CHECKPAY_RESULT_AMOUNT_MISMATCH, "Amount mismatch", {
            "can_pay": False, "contract_number": row["contract_number"],
            "expected_amount": expected_amount, "received_amount": amount, "currency": "KZT",
        })

    kaspi_status = (row["kaspi_status"] or "").upper()
    is_activated = bool(row["is_activated"])
    if is_activated or kaspi_status in KASPI_PAID_STATUSES:
        return _kaspi_response(request_id, KASPI_CHECKPAY_RESULT_ALREADY_PAID, "Already paid", {
            "can_pay": False, "contract_number": row["contract_number"],
            "amount": expected_amount, "currency": "KZT",
            "status": kaspi_status or "PAID", "is_activated": is_activated,
        })

    if phone:
        _upsert_kaspi_remote_state(
            contract_number=contract_number,
            kaspi_order_id=row["kaspi_order_id"] or "",
            kaspi_status=kaspi_status or "CREATED",
            paid_at=None,
            phone=phone,
        )

    _, _, amount_to_title, _ = get_tariff_runtime_state()
    tariff_name = amount_to_title.get(expected_amount, f"Доступ в интернет {expected_amount} ₸")

    return _kaspi_response(request_id, KASPI_CHECKPAY_RESULT_OK, "OK", {
        "can_pay": True, "contract_number": row["contract_number"],
        "amount": expected_amount, "currency": "KZT", "tariff_name": tariff_name,
        "service_name": "BusLink — интернет в автобусе",
        "status": kaspi_status or "CREATED", "router_id": row["router_id"],
        "minutes": int(row["minutes"] or 0),
    })


@router.api_route("/api/kaspi/pay", methods=["GET", "POST"])
async def kaspi_pay(request: Request):
    from datetime import datetime
    if not _has_valid_checkpay_auth(request):
        return utf8_json_response({"result": 401, "message": "Unauthorized"}, status_code=401)

    data = await _parse_kaspi_request_data(request)
    if not data:
        return _kaspi_response("-", KASPI_CHECKPAY_RESULT_INVALID_REQUEST, "Invalid request")

    request_id = _kaspi_request_id(data)
    contract_number = _normalize_contract_number(
        _pick_value(data, "contract_number", "contractNumber", "account", "account_id", "order_id", "orderId")
    )
    transaction_id = _pick_value(data, "transaction_id", "transactionId", "txn_id", "txnId", "payment_id", "paymentId")[:128]
    amount = _pick_amount_value(data, "amount", "sum", "payment_amount", "paymentAmount")
    payment_datetime = _pick_value(data, "payment_datetime", "paymentDateTime", "paid_at", "paidAt", "date")
    phone = _pick_value(data, "phone", "phone_number", "phoneNumber", "subscriber")[:32] or None

    if not contract_number:
        return _kaspi_response(request_id, KASPI_CHECKPAY_RESULT_INVALID_REQUEST, "contract_number is required")
    if amount is None or amount <= 0:
        return _kaspi_response(request_id, KASPI_CHECKPAY_RESULT_INVALID_REQUEST, "amount is required")
    if not transaction_id:
        return _kaspi_response(request_id, KASPI_CHECKPAY_RESULT_INVALID_REQUEST, "transaction_id is required")

    row = _fetch_kaspi_order_by_contract(contract_number)
    if not row:
        return _kaspi_response(request_id, KASPI_CHECKPAY_RESULT_CONTRACT_NOT_FOUND, "Contract not found")

    expected_amount = int(row["amount"] or 0)
    if amount != expected_amount:
        return _kaspi_response(request_id, KASPI_CHECKPAY_RESULT_AMOUNT_MISMATCH, "Amount mismatch", {
            "contract_number": contract_number, "expected_amount": expected_amount, "received_amount": amount,
        })

    tx_contract = _fetch_contract_by_transaction_id(transaction_id)
    if tx_contract and tx_contract != contract_number:
        return _kaspi_response(request_id, KASPI_CHECKPAY_RESULT_TX_CONFLICT,
                               "Transaction id already linked to another contract",
                               {"transaction_id": transaction_id, "contract_number": tx_contract})

    status_before = (row["kaspi_status"] or "").upper()
    is_activated_before = bool(row["is_activated"])
    already_same_tx = bool(
        (row["kaspi_order_id"] or "") == transaction_id
        and (is_activated_before or status_before in KASPI_PAID_STATUSES)
    )
    if already_same_tx:
        return _kaspi_response(request_id, KASPI_CHECKPAY_RESULT_OK, "Already processed", {
            "transaction_id": transaction_id, "contract_number": contract_number,
            "status": "DUPLICATE_OK", "is_activated": is_activated_before,
        })

    try:
        _upsert_kaspi_remote_state(
            contract_number=contract_number,
            kaspi_order_id=transaction_id,
            kaspi_status="PAID",
            paid_at=payment_datetime or datetime.utcnow().isoformat(),
            phone=phone,
        )
        _process_kaspi_paid(contract_number)
    except Exception as e:
        logger.error("[KASPI][PAY] processing failed contract=%s tx=%s err=%s",
                     contract_number, transaction_id, str(e)[:200])
        return _kaspi_response(request_id, KASPI_CHECKPAY_RESULT_INTERNAL_ERROR, "Internal processing error",
                               {"transaction_id": transaction_id, "contract_number": contract_number})

    row_after = _fetch_kaspi_order_by_contract(contract_number)
    is_activated_after = bool(row_after["is_activated"]) if row_after else False
    kaspi_status_after = ((row_after["kaspi_status"] or "PAID").upper() if row_after else "PAID")
    response_status = "ACCEPTED" if is_activated_after else "ACCEPTED_PENDING_ACTIVATION"

    return _kaspi_response(request_id, KASPI_CHECKPAY_RESULT_OK, "Payment accepted", {
        "transaction_id": transaction_id, "contract_number": contract_number,
        "amount": expected_amount, "currency": "KZT",
        "status": response_status, "kaspi_status": kaspi_status_after,
        "is_activated": is_activated_after,
    })


@router.post("/api/kaspi/sync_once")
async def kaspi_sync_trigger():
    result = await kaspi_sync_once()
    return utf8_json_response(result)
