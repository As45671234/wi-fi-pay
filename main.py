import os
import asyncio
import hashlib
import hmac
import time
import logging
import sqlite3
import re
import secrets
import json
import socket
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlencode, unquote

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, Response
from pydantic import BaseModel

from kaspi_client import KaspiApiClient, KaspiClientError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import routeros_api

load_dotenv()


# --- НАСТРОЙКИ ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("WiFiGateway")

app = FastAPI(title="Wi-Fi Gateway Final")


def make_cid() -> str:
    return secrets.token_hex(6)


@app.middleware("http")
async def request_timing_middleware(request: Request, call_next):
    started = time.monotonic()
    cid = (request.query_params.get("cid") or "-")[:24]
    ip = get_client_ip(request)
    ua = (request.headers.get("user-agent") or "")[:120]
    method = request.method
    path = request.url.path
    query = request.url.query
    try:
        response = await call_next(request)
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.info(
            f"[HTTP] cid={cid} ip={ip} {method} {path}"
            f"{'?' + query if query else ''} -> {response.status_code} in {elapsed_ms:.0f}ms ua='{ua}'"
        )
        return response
    except Exception as e:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.exception(f"[HTTP] cid={cid} ip={ip} {method} {path} failed in {elapsed_ms:.0f}ms: {e}")
        raise

def utf8_json_response(content, status_code=200):
    return JSONResponse(content, status_code=status_code, headers={"Content-Type": "application/json; charset=utf-8"})

# --- ЗАГРУЗКА КОНФИГА РОУТЕРОВ ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROUTERS_CONFIG_PATH = os.path.join(BASE_DIR, "routers_config.json")
TARIFFS_CONFIG_PATH = os.path.join(BASE_DIR, "tariffs_config.json")

if os.path.exists(ROUTERS_CONFIG_PATH):
    with open(ROUTERS_CONFIG_PATH, encoding="utf-8") as f:
        routers_list = json.load(f)
    ROUTERS_CONFIG = {router["id"]: router for router in routers_list}
else:
    ROUTERS_CONFIG = {}


def _hours_word(hours: int) -> str:
    mod10 = hours % 10
    mod100 = hours % 100
    if mod10 == 1 and mod100 != 11:
        return "час"
    if mod10 in (2, 3, 4) and mod100 not in (12, 13, 14):
        return "часа"
    return "часов"


def _default_tariff_title(minutes: int) -> str:
    if minutes >= 1440:
        return "До конца поездки"
    if minutes % 60 == 0 and minutes >= 60:
        h = minutes // 60
        return f"{h} {_hours_word(h)} доступа"
    return f"{minutes} минут доступа"


def _default_tariff_subtitle(minutes: int) -> str:
    if minutes >= 1440:
        return "Максимум комфорта"
    if minutes % 60 == 0 and minutes >= 60:
        h = minutes // 60
        return f"Доступ на {h} {_hours_word(h)}"
    return f"Доступ на {minutes} минут"


def _normalize_tariff(raw: dict) -> dict | None:
    try:
        amount = int(raw.get("amount"))
        minutes = int(raw.get("minutes"))
    except Exception:
        return None

    if amount <= 0 or minutes <= 0:
        return None

    title = (raw.get("title") or "").strip() or _default_tariff_title(minutes)
    subtitle = (raw.get("subtitle") or "").strip() or _default_tariff_subtitle(minutes)
    badge = (raw.get("badge") or "").strip()
    return {
        "amount": amount,
        "minutes": minutes,
        "title": title,
        "subtitle": subtitle,
        "badge": badge,
    }


def load_tariffs_config() -> list[dict]:
    default = [
        {"amount": 500, "minutes": 60, "title": "1 час доступа", "subtitle": "Час бесплатно¯", "badge": "Популярное"},
        {"amount": 1000, "minutes": 180, "title": "3 часа доступа", "subtitle": "Час бесплатно* * * *", "badge": ""},
        {"amount": 1500, "minutes": 1440, "title": "До конца поездки", "subtitle": "Час бесплатно *", "badge": "Выгодно!"},
    ]

    raw_items = default
    if os.path.exists(TARIFFS_CONFIG_PATH):
        try:
            with open(TARIFFS_CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                raw_items = data
        except Exception as e:
            logger.warning(f"Не удалось прочитать tariffs_config.json, используем дефолт: {e}")

    normalized = []
    seen_amounts = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        t = _normalize_tariff(item)
        if not t:
            continue
        if t["amount"] in seen_amounts:
            continue
        seen_amounts.add(t["amount"])
        normalized.append(t)

    if not normalized:
        normalized = [
            _normalize_tariff(x) for x in default
            if _normalize_tariff(x) is not None
        ]

    normalized.sort(key=lambda x: x["amount"])
    return normalized


def get_tariff_runtime_state() -> tuple[list[dict], dict[int, int], dict[int, str], list[int]]:
    tariffs = load_tariffs_config()
    amount_to_minutes = {t["amount"]: t["minutes"] for t in tariffs}
    amount_to_title = {t["amount"]: t["title"] for t in tariffs}
    allowed_amounts = sorted(amount_to_minutes.keys())
    return tariffs, amount_to_minutes, amount_to_title, allowed_amounts

MERCHANT_ID = os.getenv("MERCHANT_ID") or os.getenv("FREEDOMPAY_MERCHANT_ID") or ""
SECRET_KEY = os.getenv("SECRET_KEY") or os.getenv("FREEDOMPAY_SECRET_KEY") or ""
PAY_URL = os.getenv("PAY_URL") or os.getenv("FREEDOMPAY_API_URL") or "https://api.freedompay.kz/payment.php"
KZ_TZ = ZoneInfo("Asia/Almaty")
TRIAL_TOKEN_TTL_SECONDS = 5 * 60
TRIAL_RATE_LIMIT_WINDOW_SECONDS = 10 * 60
TRIAL_RATE_LIMIT_MAX_REQUESTS = 6
TRIAL_RATE_BUCKET = {}
PREPARE_TIMEOUT_SECONDS = 10

KASPI_ENABLED = (os.getenv("KASPI_ENABLED", "false").strip().lower() == "true")
KASPI_API_BASE_URL = os.getenv("KASPI_API_BASE_URL", "").strip()
KASPI_API_TOKEN = os.getenv("KASPI_API_TOKEN", "").strip()
KASPI_ORDERS_PATH = os.getenv("KASPI_ORDERS_PATH", "/orders").strip()
KASPI_ORDER_DETAILS_PATH = os.getenv("KASPI_ORDER_DETAILS_PATH", "/orders/{order_id}").strip()
KASPI_API_TIMEOUT_SECONDS = int(os.getenv("KASPI_API_TIMEOUT_SECONDS", "10") or "10")
KASPI_SYNC_INTERVAL_SECONDS = max(5, int(os.getenv("KASPI_SYNC_INTERVAL_SECONDS", "30") or "30"))
KASPI_MATCH_WINDOW_MINUTES = max(10, int(os.getenv("KASPI_MATCH_WINDOW_MINUTES", "720") or "720"))
KASPI_ACTIVATION_RETRY_DELAY_SECONDS = max(10, int(os.getenv("KASPI_ACTIVATION_RETRY_DELAY_SECONDS", "60") or "60"))
KASPI_MAX_ACTIVATION_RETRIES = max(1, int(os.getenv("KASPI_MAX_ACTIVATION_RETRIES", "5") or "5"))

KASPI_PAID_STATUSES = {
    s.strip().upper()
    for s in os.getenv("KASPI_PAID_STATUSES", "PAID,SUCCESS,DONE").split(",")
    if s.strip()
}
KASPI_FAILED_STATUSES = {
    s.strip().upper()
    for s in os.getenv("KASPI_FAILED_STATUSES", "CANCELLED,CANCELED,FAILED,REJECTED").split(",")
    if s.strip()
}
KASPI_PENDING_STATUSES = {
    s.strip().upper()
    for s in os.getenv("KASPI_PENDING_STATUSES", "NEW,CREATED,PENDING,PROCESSING,WAITING").split(",")
    if s.strip()
}

kaspi_sync_task: asyncio.Task | None = None
kaspi_sync_stop = asyncio.Event()
kaspi_sync_lock = asyncio.Lock()
kaspi_activation_db_lock = threading.Lock()

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
STATIC_DIR = os.path.join(BASE_DIR, "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
else:
    logger.warning(f"Static directory not found, skipping mount: {STATIC_DIR}")

# --- БАЗА ДАННЫХ ---
DB_PATH = os.path.join(BASE_DIR, 'gateway.db')

def get_db() -> sqlite3.Connection:
    """Возвращает connection с WAL mode и timeout для безопасной работы с несколькими workers."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def init_db():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mac_address TEXT,
            amount INTEGER,
            status TEXT,
            router_id TEXT,
            device_id TEXT,
            payment_order_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(orders)")
    columns = {row[1] for row in cursor.fetchall()}
    if 'device_id' not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN device_id TEXT")
    if 'payment_order_id' not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN payment_order_id TEXT")
    if 'expires_at' not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN expires_at TIMESTAMP")

    conn.execute('''
        CREATE TABLE IF NOT EXISTS kaspi_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            local_order_id TEXT,
            contract_number TEXT,
            external_order_ref TEXT,
            mac_address TEXT NOT NULL,
            router_id TEXT NOT NULL,
            amount INTEGER NOT NULL,
            minutes INTEGER NOT NULL,
            kaspi_order_id TEXT,
            kaspi_status TEXT,
            paid_at TIMESTAMP,
            activated_at TIMESTAMP,
            is_activated INTEGER NOT NULL DEFAULT 0,
            activation_lock INTEGER NOT NULL DEFAULT 0,
            activation_attempts INTEGER NOT NULL DEFAULT 0,
            activation_error TEXT,
            last_activation_attempt_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_kaspi_orders_contract_number ON kaspi_orders(contract_number)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_kaspi_orders_kaspi_order_id ON kaspi_orders(kaspi_order_id) WHERE kaspi_order_id IS NOT NULL AND kaspi_order_id <> ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kaspi_orders_status ON kaspi_orders(kaspi_status, is_activated)")

    cursor.execute("PRAGMA table_info(kaspi_orders)")
    k_columns = {row[1] for row in cursor.fetchall()}
    if 'external_order_ref' not in k_columns:
        conn.execute("ALTER TABLE kaspi_orders ADD COLUMN external_order_ref TEXT")
    if 'activation_lock' not in k_columns:
        conn.execute("ALTER TABLE kaspi_orders ADD COLUMN activation_lock INTEGER NOT NULL DEFAULT 0")
    if 'activation_attempts' not in k_columns:
        conn.execute("ALTER TABLE kaspi_orders ADD COLUMN activation_attempts INTEGER NOT NULL DEFAULT 0")
    if 'activation_error' not in k_columns:
        conn.execute("ALTER TABLE kaspi_orders ADD COLUMN activation_error TEXT")
    if 'last_activation_attempt_at' not in k_columns:
        conn.execute("ALTER TABLE kaspi_orders ADD COLUMN last_activation_attempt_at TIMESTAMP")

    conn.commit()
    conn.close()

def get_or_create_device_id(request: Request):
    device_id = request.cookies.get("wf_device_id")
    if device_id:
        return device_id, False
    return secrets.token_hex(16), True

def check_trial_used_last_24h(mac: str, device_id: str) -> bool:
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id FROM orders 
            WHERE (mac_address = ? OR device_id = ?) 
              AND status = 'TRIAL' 
              AND created_at > datetime('now', '-1 day')
            """, (mac, device_id)
        )
        return cursor.fetchone() is not None
    finally:
        conn.close()

def get_client_ip(request: Request) -> str:
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        return xff.split(",")[0].strip()
    client = request.client
    return (client.host if client else "unknown") or "unknown"

def is_trial_rate_limited(request: Request) -> bool:
    ip = get_client_ip(request)
    now = int(time.time())
    recent =[
        ts for ts in TRIAL_RATE_BUCKET.get(ip, [])
        if now - ts < TRIAL_RATE_LIMIT_WINDOW_SECONDS
    ]
    if len(recent) >= TRIAL_RATE_LIMIT_MAX_REQUESTS:
        TRIAL_RATE_BUCKET[ip] = recent
        return True
    recent.append(now)
    TRIAL_RATE_BUCKET[ip] = recent
    return False

def make_trial_signature(mac: str, router_id: str, trial_ts: str) -> str:
    payload = f"{mac}|{router_id}|{trial_ts}"
    return hmac.new(SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()

def is_valid_trial_signature(mac: str, router_id: str, trial_ts: str, trial_sig: str) -> bool:
    if not trial_ts or not re.fullmatch(r"\d{10}", trial_ts):
        return False
    if not trial_sig or not re.fullmatch(r"[0-9a-fA-F]{64}", trial_sig):
        return False
    now = int(time.time())
    ts = int(trial_ts)
    if abs(now - ts) > TRIAL_TOKEN_TTL_SECONDS:
        return False
    expected = make_trial_signature(mac, router_id, trial_ts)
    return hmac.compare_digest(expected, trial_sig.lower())

init_db()


class KaspiCreateOrderRequest(BaseModel):
    amount: int
    mac: str
    router_id: str = "astana_01"
    cid: str = ""


class KaspiStatusResponse(BaseModel):
    contract_number: str
    kaspi_status: str
    is_activated: bool
    paid_at: str | None = None
    activated_at: str | None = None
    amount: int
    minutes: int


def _normalize_mac(mac: str) -> str:
    return (mac or "").upper()


def _is_valid_mac(mac: str) -> bool:
    return bool(re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", _normalize_mac(mac)))


def make_contract_number(mac: str) -> str:
    mac_norm = _normalize_mac(mac)
    if not _is_valid_mac(mac_norm):
        raise ValueError("Некорректный MAC")
    mac_hex = mac_norm.replace(":", "")
    ts = int(time.time())
    nonce = secrets.token_hex(2).upper()
    return f"A13{mac_hex}{ts}{nonce}"


def parse_contract_number(contract_number: str) -> tuple[str, bool]:
    raw = (contract_number or "").strip().upper()
    m = re.fullmatch(r"A13([0-9A-F]{12})(\d{10})([0-9A-F]{4})", raw)
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


def _select_local_candidates_for_contract(contract_number: str, amount: int | None) -> list[sqlite3.Row]:
    conn = get_db()
    conn.row_factory = sqlite3.Row
    try:
        args: list = [contract_number]
        sql = """
            SELECT * FROM kaspi_orders
            WHERE contract_number = ?
        """
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
                updated_at = CURRENT_TIMESTAMP
            WHERE contract_number = ?
            """,
            (kaspi_order_id, kaspi_order_id, kaspi_status, paid_at, contract_number),
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


@app.on_event("startup")
async def _startup_kaspi_sync():
    global kaspi_sync_task
    kaspi_sync_stop.clear()
    if KASPI_ENABLED:
        kaspi_sync_task = asyncio.create_task(_kaspi_sync_loop())
        logger.info("[KASPI] feature enabled")
    else:
        logger.info("[KASPI] feature disabled")


@app.on_event("shutdown")
async def _shutdown_kaspi_sync():
    global kaspi_sync_task
    kaspi_sync_stop.set()
    if kaspi_sync_task:
        try:
            await asyncio.wait_for(kaspi_sync_task, timeout=5)
        except Exception:
            pass
        kaspi_sync_task = None


# --- ДИАГНОСТИКА РОУТЕРОВ ---

def verify_access_activated(api, mac: str, user_name: str, mode: str) -> dict:
    """Проверяет что доступ РЕАЛЬНО был активирован на MikroTik"""
    result = {
        "binding_exists": False,
        "user_exists": False,
        "mode": mode
    }
    
    try:
        binding = api.get_resource('/ip/hotspot/ip-binding')
        bindings = binding.call('print', queries={'mac-address': mac})
        if bindings:
            for b in bindings:
                comment = b.get('comment', '')
                if mode in comment:
                    result["binding_exists"] = True
                    logger.debug(f"  ✓ Биндинг найден: {comment}")
                    break
        else:
            logger.debug(f"  ⚠️ Биндинга не найдено для {mac}")
    except Exception as e:
        logger.warning(f"  Ошибка проверки биндинга: {str(e)[:100]}")

    try:
        user_res = api.get_resource('/ip/hotspot/user')
        users = user_res.call('print', queries={'name': user_name})
        if users:
            for u in users:
                comment = u.get('comment', '')
                if mode in comment:
                    result["user_exists"] = True
                    logger.debug(f"  ✓ Юзер найден: {comment}")
                    break
        else:
            logger.debug(f"  ⚠️ Юзера не найдено: {user_name}")
    except Exception as e:
        logger.warning(f"  Ошибка проверки юзера: {str(e)[:100]}")

    return result

def check_router_hotspot_enabled(config: dict) -> bool:
    """Проверяет что hotspot включен на роутере"""
    try:
        connection = routeros_api.RouterOsApiPool(
            config['ip'],
            username=config['user'],
            password=config['pass'],
            port=config.get('port', 8728),
            plaintext_login=True,
        )
        api = connection.get_api()
        
        # Проверяем статус hotspot
        hotspot_profiles = api.get_resource('/ip/hotspot/profile').call('print')
        if not hotspot_profiles:
            logger.error(f"  ❌ На роутере {config['ip']} нет профилей hotspot!")
            connection.disconnect()
            return False
        
        logger.info(f"  ✓ Hotspot профили найдены на {config['ip']}")
        connection.disconnect()
        return True
    except Exception as e:
        logger.error(f"  ❌ Не удалось проверить hotspot на {config['ip']}: {str(e)[:150]}")
        return False

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ MIKROTIK ---

def _mikrotik_check_existing_access(binding, user_res, user_name, mac):
    """Проверяет наличие PAID/TRIAL. Возвращает True если уже есть активный доступ."""
    for b in binding.call('print', queries={'mac-address': mac}):
        comment = (b.get('comment') or '')
        if comment.startswith('TRIAL_') or comment.startswith('PAID_'):
            logger.info(f"Защита: {comment} уже активен для {mac[:8]}***, не перетирать")
            return True
    for u in user_res.call('print', queries={'name': user_name}):
        comment = (u.get('comment') or '')
        if comment.startswith('TRIAL_') or comment.startswith('PAID_'):
            logger.info(f"Защита: {comment} юзер уже активен для {mac[:8]}***, не перетирать")
            return True
    return False


def _mikrotik_cleanup_old(binding, active, user_res, mac, user_name, mode):
    """Удаляет старые сессии для MAC (биндинги, active, юзеры)."""
    for b in binding.call('print', queries={'mac-address': mac}):
        comment = (b.get('comment') or '')
        if mode == 'PAY_WINDOW' and (comment.startswith('PAID_') or comment.startswith('TRIAL_')):
            logger.info(f"PAY_WINDOW: защита {comment} - не удаляем")
            continue
        try:
            binding.call('remove', arguments={'.id': b.get('id') or b.get('.id')})
            logger.debug(f"Удален биндинг {mac}: {comment}")
        except Exception as e:
            logger.warning(f"Ошибка при удалении биндинга {mac}: {str(e)[:100]}")

    for a in active.call('print', queries={'mac-address': mac}):
        try:
            active.call('remove', arguments={'.id': a.get('id') or a.get('.id')})
        except Exception as e:
            logger.warning(f"Ошибка при удалении active {mac}: {str(e)[:100]}")

    for u in user_res.call('print', queries={'name': user_name}):
        try:
            user_res.call('remove', arguments={'.id': u.get('id') or u.get('.id')})
        except Exception as e:
            logger.warning(f"Ошибка при удалении юзера {user_name}: {str(e)[:100]}")


def _mikrotik_create_access(binding, active, user_res, host_res, mac, user_name, user_pass, minutes, mode):
    """Создаёт доступ: binding для PAY_WINDOW/TRIAL, user+login для PAID."""
    if mode in ('PAY_WINDOW', 'TRIAL'):
        try:
            binding.call('add', arguments={'mac-address': mac, 'type': 'bypassed', 'comment': f"{mode}_{mac}"})
            logger.info(f"✓ Биндинг {mode} добавлен для {mac[:8]}***")
        except Exception as e:
            logger.error(f"❌ ОШИБКА добавления биндинга {mode} для {mac}: {str(e)[:150]}")
            raise
    else:
        user_res.call('add', arguments={
            'name': user_name,
            'password': user_pass,
            'limit-uptime': f"{minutes}m",
            'comment': f"{mode}_{mac}",
        })

        host_ip = None
        access_mode = "BYPASS"

        for _ in range(5):
            hosts = host_res.call('print', queries={'mac-address': mac})
            if hosts:
                host_ip = hosts[0].get('address')
            if host_ip:
                login_args = {'user': user_name, 'password': user_pass, 'mac-address': mac, 'ip': host_ip}
                try:
                    active.call('login', arguments=login_args)
                    time.sleep(0.25)
                    active_rows = active.call('print', queries={'mac-address': mac})
                    if active_rows:
                        access_mode = "ACTIVE"
                        break
                except Exception:
                    pass
            time.sleep(0.35)

        # Для PAID всегда держим bypass binding как страховку.
        # Это защищает iPhone-сценарий, когда active-сессия может исчезнуть,
        # и гарантирует, что PAY_WINDOW не перетрёт уже оплаченный доступ.
        paid_binding_exists = False
        for b in binding.call('print', queries={'mac-address': mac}):
            comment = (b.get('comment') or '')
            if comment.startswith(f"{mode}_"):
                paid_binding_exists = True
                break

        if not paid_binding_exists:
            try:
                binding.call('add', arguments={'mac-address': mac, 'type': 'bypassed', 'comment': f"{mode}_{mac}"})
                if access_mode == "ACTIVE":
                    logger.info(f"PAID страховка: добавлен BYPASS биндинг для {mac[:8]}***")
                else:
                    logger.info(f"Fallback: биндинг BYPASS добавлен для {mac[:8]}***")
            except Exception as e:
                logger.error(f"❌ Ошибка добавления BYPASS-биндинга для {mac}: {str(e)[:150]}")


def _mikrotik_setup_scheduler(api, sched, mac, user_name, mode, seconds, minutes):
    """Настраивает scheduler на MikroTik для автоочистки доступа."""
    try:
        clock_info = api.get_resource('/system/clock').call('print')[0]
        date_str = clock_info.get('date', '')
        time_str = clock_info.get('time', '')
        # MikroTik может вернуть дату в разных форматах
        mt_now = None
        for fmt in ("%b/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                mt_now = datetime.strptime(f"{date_str} {time_str}", fmt)
                break
            except ValueError:
                try:
                    mt_now = datetime.strptime(f"{date_str.title()} {time_str}", fmt)
                    break
                except ValueError:
                    continue
        if mt_now is None:
            raise ValueError(f"Unknown clock format: '{date_str}' '{time_str}'")
    except Exception as e:
        logger.error(f"Clock parse error: {e}")
        mt_now = datetime.now(KZ_TZ).replace(tzinfo=None)

    duration_seconds = max(1, int(seconds if seconds is not None else round(minutes * 60)))
    mt_expiry = mt_now + timedelta(seconds=duration_seconds)
    mt_date = mt_expiry.strftime("%b/%d/%Y").lower()
    mt_time = mt_expiry.strftime("%H:%M:%S")
    task_name = f"del_{mac.replace(':', '')}"

    on_event = (
        f':do {{ /ip hotspot active remove [find mac-address="{mac}"] }} on-error={{}}; '
        f':do {{ /ip hotspot cookie remove [find mac-address="{mac}"] }} on-error={{}}; '
        f':do {{ /ip hotspot host remove [find mac-address="{mac}"] }} on-error={{}}; '
        f':do {{ /ip hotspot ip-binding remove [find mac-address="{mac}"] }} on-error={{}}; '
        f':do {{ /ip hotspot user remove [find name="{user_name}"] }} on-error={{}}; '
        f':do {{ /system scheduler remove [find name="{task_name}"] }} on-error={{}}; '
    )

    for t in sched.call('print', queries={'name': task_name}):
        try:
            sched.call('remove', arguments={'.id': t.get('id') or t.get('.id')})
            logger.debug(f"Старый scheduler {task_name} удален")
        except Exception as e:
            logger.warning(f"Ошибка удаления старого scheduler {task_name}: {str(e)[:100]}")

    try:
        sched.call('add', arguments={
            'name': task_name,
            'start-date': mt_date,
            'start-time': mt_time,
            'interval': "00:00:00",
            'on-event': on_event,
            'comment': f"AUTOCLEAR_{mode}_{mac}",
        })
        logger.info(f"✓ Scheduler {task_name} установлен на {mt_date} {mt_time} для очистки {mode} через {duration_seconds}с")
    except Exception as e:
        logger.error(f"❌ КРИТИЧЕСКАЯ ОШИБКА при создании scheduler {task_name}: {str(e)[:150]}")
        raise


# --- ЯДРО: MIKROTIK API ---

def _router_api_reachable(ip: str, port: int, timeout_sec: float = 0.8) -> bool:
    """Быстрый precheck TCP до RouterOS API, чтобы не зависать на долгом connect timeout."""
    try:
        with socket.create_connection((ip, port), timeout=timeout_sec):
            return True
    except OSError:
        return False

def set_mikrotik_ah_access(mac: str, router_id: str, minutes: int, mode: str, seconds: int | None = None):
    config = ROUTERS_CONFIG.get(router_id)
    if not config:
        logger.error(f"❌ Неизвестный router_id: {router_id}")
        return False

    if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac or ""):
        logger.error(f"❌ Некорректный MAC: {mac}")
        return False

    connection = None
    # Для PAY_WINDOW держим fail-fast, чтобы UX не зависал на десятки секунд.
    max_retries = 1 if mode == 'PAY_WINDOW' else 3
    api_port = int(config.get('port', 8728))

    # Fail-fast: если API-порт недоступен, не тратим десятки секунд на retries библиотеки.
    if mode == 'PAY_WINDOW' and not _router_api_reachable(config['ip'], api_port):
        logger.error(f"[MK] API недоступен {router_id} ({config['ip']}:{api_port})")
        return False

    for attempt in range(1, max_retries + 1):
        try:
            t0 = time.monotonic()
            connection = routeros_api.RouterOsApiPool(
                config['ip'],
                username=config['user'],
                password=config['pass'],
                port=api_port,
                plaintext_login=True,
            )
            api = connection.get_api()
            logger.info(f"[MK] connect {router_id}: {(time.monotonic()-t0)*1000:.0f}ms (attempt {attempt})")

            # Быстрый путь для PAY_WINDOW: биндинг + scheduler, без cleanup юзеров/active
            if mode == 'PAY_WINDOW':
                t1 = time.monotonic()
                binding = api.get_resource('/ip/hotspot/ip-binding')
                user_res = api.get_resource('/ip/hotspot/user')
                active = api.get_resource('/ip/hotspot/active')
                sched = api.get_resource('/system/scheduler')
                user_name = f"T-{mac.replace(':', '')}"

                # ВАЖНО: оплаченный доступ может быть как в ip-binding, так и в hotspot user.
                # Если защищать только binding, PAY_WINDOW способен перетереть PAID/TRIAL у iPhone.
                for b in binding.call('print', queries={'mac-address': mac}):
                    comment = (b.get('comment') or '')
                    if comment.startswith('PAID_') or comment.startswith('TRIAL_'):
                        logger.info(f"PAY_WINDOW: уже есть {comment} для {mac[:8]}***, пропускаем")
                        return True
                    try:
                        binding.call('remove', arguments={'.id': b.get('id') or b.get('.id')})
                    except Exception:
                        pass

                for u in user_res.call('print', queries={'name': user_name}):
                    comment = (u.get('comment') or '')
                    if comment.startswith('PAID_') or comment.startswith('TRIAL_'):
                        # Считаем доступ реально защищенным только если есть active по MAC
                        # или защищенный binding. Иначе это может быть "зависший" user.
                        has_active = False
                        has_protected_binding = False
                        try:
                            has_active = bool(active.call('print', queries={'mac-address': mac}))
                        except Exception:
                            has_active = False

                        if not has_active:
                            try:
                                for pb in binding.call('print', queries={'mac-address': mac}):
                                    pcomment = (pb.get('comment') or '')
                                    if pcomment.startswith('PAID_') or pcomment.startswith('TRIAL_'):
                                        has_protected_binding = True
                                        break
                            except Exception:
                                has_protected_binding = False

                        if has_active or has_protected_binding:
                            logger.info(f"PAY_WINDOW: уже есть user {comment} для {mac[:8]}***, пропускаем")
                            return True

                        logger.info(f"PAY_WINDOW: найден stale user {comment} для {mac[:8]}***, продолжаем создание PAY_WINDOW")

                logger.info(f"[MK] binding print+cleanup: {(time.monotonic()-t1)*1000:.0f}ms")
                t2 = time.monotonic()
                binding.call('add', arguments={'mac-address': mac, 'type': 'bypassed', 'comment': f"PAY_WINDOW_{mac}"})
                logger.info(f"[MK] binding add: {(time.monotonic()-t2)*1000:.0f}ms")
                t3 = time.monotonic()
                _mikrotik_setup_scheduler(api, sched, mac, user_name, "PAY_WINDOW", seconds, minutes)
                logger.info(f"[MK] scheduler: {(time.monotonic()-t3)*1000:.0f}ms, PAY_WINDOW total: {(time.monotonic()-t0)*1000:.0f}ms")
                return True

            binding = api.get_resource('/ip/hotspot/ip-binding')
            active = api.get_resource('/ip/hotspot/active')
            user_res = api.get_resource('/ip/hotspot/user')
            host_res = api.get_resource('/ip/hotspot/host')
            sched = api.get_resource('/system/scheduler')

            user_name = f"T-{mac.replace(':', '')}"
            user_pass = f"p{int(time.time()) % 1000000}"

            _mikrotik_cleanup_old(binding, active, user_res, mac, user_name, mode)
            _mikrotik_create_access(binding, active, user_res, host_res, mac, user_name, user_pass, minutes, mode)
            _mikrotik_setup_scheduler(api, sched, mac, user_name, mode, seconds, minutes)

            # Верификация только для PAID — для PAY_WINDOW/TRIAL лишние round-trip не нужны
            if mode == 'PAID':
                logger.info(f"[VERIFY] Проверяю что {mode} реально активирован...")
                verify_result = verify_access_activated(api, mac, user_name, mode)
                if verify_result["binding_exists"] or verify_result["user_exists"]:
                    logger.info(f"✅ ПОДТВЕРЖЕНО: {mode} активирован для {mac[:8]}*** ({'биндинг' if verify_result['binding_exists'] else 'юзер'})")
                else:
                    logger.error(f"⚠️ ВНИМАНИЕ: {mode} МОЖЕТ НЕ АКТИВИРОВАН для {mac[:8]}*** (проверить на роутере вручную!)")

            if mode in ['PAID', 'PAY_WINDOW']:
                logger.info(f"Access granted: {mode} {minutes}min for {mac[:8]}***")
            return True
        except Exception as e:
            logger.error(f"MikroTik API error for {router_id} (attempt {attempt}/{max_retries}): {str(e)[:200]}")
            if connection:
                try:
                    connection.disconnect()
                except Exception:
                    pass
            if attempt == max_retries:
                logger.error(f"❌ Не удалось подключиться к роутеру {router_id} после {max_retries} попыток.")
                return False
            time.sleep(1)
        finally:
            if connection:
                try:
                    connection.disconnect()
                except Exception:
                    pass

# --- МАРШРУТЫ ---

def get_signature(script_name, params, secret_key):
    sorted_keys = sorted(params.keys())
    values = [str(params[k]) for k in sorted_keys if k != 'pg_sig' and params[k] is not None]
    sig_str = f"{script_name};{';'.join(values)};{secret_key}"
    return hashlib.md5(sig_str.encode('utf-8')).hexdigest()

def decode_nested_url_value(value: str) -> str:
    if value is None:
        return ""
    decoded = value
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return decoded

def build_payment_url(amount: int, mac: str, router_id: str, payment_order_id: str, cid: str = "") -> str:
    _, amount_to_minutes, _, _ = get_tariff_runtime_state()
    minutes = amount_to_minutes.get(amount, 60)
    cid = (cid or make_cid())[:24]
    success_url = (
        f"https://wifi-pay.kz/success"
        f"?mac={mac}"
        f"&router_id={router_id}"
        f"&minutes={minutes}"
        f"&amount={amount}"
        f"&cid={cid}"
    )
    params = {
        'pg_merchant_id': MERCHANT_ID, 'pg_amount': str(amount), 'pg_currency': 'KZT',
        'pg_description': f"Wi-Fi {mac}", 'pg_order_id': payment_order_id,
        'pg_salt': 'salt', 'pg_param1': mac, 'pg_param2': router_id, 'pg_param3': cid,
        'pg_result_url': 'https://wifi-pay.kz/payment_result',
        'pg_success_url': success_url,
    }
    params['pg_sig'] = get_signature("payment.php", params, SECRET_KEY)
    return f"{PAY_URL}?{urlencode(params)}"

@app.get("/session_status")
async def session_status(mac: str, router_id: str = "astana_01"):
    """Возвращает статус сессии по MAC. Используется клиентом для поллинга."""
    if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac or ""):
        return utf8_json_response({"active": False, "expires_in": -1, "status": "NONE"})
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT status, expires_at FROM orders
               WHERE mac_address=? AND router_id=? AND status IN ('PAY_WINDOW','TRIAL','PAID')
               ORDER BY
                 CASE status
                   WHEN 'PAID' THEN 0
                   WHEN 'TRIAL' THEN 1
                   WHEN 'PAY_WINDOW' THEN 2
                   ELSE 3
                 END,
                 created_at DESC
               LIMIT 1""",
            (mac, router_id)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return utf8_json_response({"active": False, "expires_in": -1, "status": "NONE"})
    status, expires_at_str = row
    if not expires_at_str:
        return utf8_json_response({"active": True, "expires_in": -1, "status": status})
    try:
        expires_at = datetime.fromisoformat(expires_at_str)
        expires_in = int((expires_at - datetime.utcnow()).total_seconds())
        return utf8_json_response({"active": expires_in > 0, "expires_in": expires_in, "status": status})
    except Exception:
        return utf8_json_response({"active": False, "expires_in": -1, "status": status})

@app.post("/api/prepare_access")
async def prepare_access(request: Request):
    """Создаёт PAY_WINDOW синхронно — welcome.html ждёт завершения через AJAX."""
    t_start = time.monotonic()
    data = await request.json()
    mac = data.get("mac", "")
    router_id = data.get("router_id", "astana_01")
    cid = (data.get("cid") or "-")[:24]

    logger.info(f"[prepare_access] START cid={cid} mac={mac[:8]}*** router={router_id}")

    if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac or ""):
        return utf8_json_response({"ok": False, "error": "Некорректный MAC"}, status_code=400)
    if router_id not in ROUTERS_CONFIG:
        return utf8_json_response({"ok": False, "error": "Неизвестный роутер"}, status_code=400)

    t_mk = time.monotonic()
    try:
        ok = await asyncio.wait_for(
            asyncio.to_thread(set_mikrotik_ah_access, mac, router_id, 3, "PAY_WINDOW"),
            timeout=PREPARE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error(f"[prepare_access] TIMEOUT cid={cid} >{PREPARE_TIMEOUT_SECONDS}s для {mac[:8]}*** ({router_id})")
        return utf8_json_response(
            {"ok": False, "error": "Роутер отвечает слишком долго. Повторите через пару секунд."},
            status_code=504,
        )

    if not ok:
        logger.error(f"[prepare_access] PAY_WINDOW FAIL cid={cid} для {mac[:8]}*** ({router_id})")
        return utf8_json_response(
            {"ok": False, "error": "Не удалось подготовить доступ. Повторите попытку."},
            status_code=502,
        )

    t_db = time.monotonic()
    expires_at = (datetime.utcnow() + timedelta(seconds=180)).isoformat()
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO orders (mac_address, amount, status, router_id, expires_at) VALUES (?, 0, 'PAY_WINDOW', ?, ?)",
            (mac, router_id, expires_at),
        )
        conn.commit()
    finally:
        conn.close()
    logger.info(f"[prepare_access] DB INSERT cid={cid}: {(time.monotonic()-t_db)*1000:.0f}ms")

    logger.info(f"[prepare_access] DONE cid={cid} MikroTik: {(time.monotonic()-t_mk)*1000:.0f}ms, total: {(time.monotonic()-t_start)*1000:.0f}ms, {'✓' if ok else '✗'} для {mac[:8]}***")
    return utf8_json_response({"ok": ok})


@app.get("/", response_class=HTMLResponse)
async def welcome(request: Request, mac: str = "00:00:00:00:00:00", router_id: str = "astana_01", cid: str = ""):
    cid = (cid or make_cid())[:24]
    logger.info(f"[welcome] cid={cid} mac={mac[:8]}*** router={router_id}")
    response = templates.TemplateResponse("welcome.html", {"request": request, "mac": mac, "router_id": router_id, "cid": cid})
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


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


@app.get("/prepare_and_tariffs", response_class=HTMLResponse)
async def prepare_and_tariffs(request: Request, mac: str, router_id: str = "astana_01", cid: str = ""):
    """Сначала готовим PAY_WINDOW, затем редиректим на /tariffs."""
    cid = (cid or make_cid())[:24]
    logger.info(f"[prepare_and_tariffs] START cid={cid} mac={mac[:8]}*** router={router_id}")

    if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac or ""):
        return templates.TemplateResponse("welcome.html", {
            "request": request,
            "mac": mac,
            "router_id": router_id,
            "cid": cid,
            "error": "Некорректный MAC",
        })
    if router_id not in ROUTERS_CONFIG:
        return templates.TemplateResponse("welcome.html", {
            "request": request,
            "mac": mac,
            "router_id": router_id,
            "cid": cid,
            "error": "Неизвестный роутер",
        })

    t_start = time.monotonic()
    try:
        ok = await asyncio.wait_for(
            asyncio.to_thread(set_mikrotik_ah_access, mac, router_id, 3, "PAY_WINDOW"),
            timeout=PREPARE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error(f"[prepare_and_tariffs] TIMEOUT cid={cid} >{PREPARE_TIMEOUT_SECONDS}s для {mac[:8]}*** ({router_id})")
        return utf8_json_response(
            {"ok": False, "error": "Роутер отвечает слишком долго. Повторите через пару секунд."},
            status_code=504,
        )

    if not ok:
        logger.error(f"[prepare_and_tariffs] PAY_WINDOW FAIL cid={cid} для {mac[:8]}*** ({router_id})")
        return utf8_json_response(
            {"ok": False, "error": "Не удалось подготовить доступ. Повторите попытку."},
            status_code=502,
        )

    expires_at = (datetime.utcnow() + timedelta(seconds=180)).isoformat()
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO orders (mac_address, amount, status, router_id, expires_at) VALUES (?, 0, 'PAY_WINDOW', ?, ?)",
            (mac, router_id, expires_at),
        )
        conn.commit()
    finally:
        conn.close()

    total_ms = (time.monotonic() - t_start) * 1000
    user_agent = (request.headers.get("user-agent") or "")
    is_android = bool(re.search(r"android", user_agent, re.IGNORECASE))

    # Android: после появления интернета captive flow часто залипает на тяжелом HTML.
    # Отдаем легкий bridge, который сразу переводит на /tariffs.
    if is_android:
        logger.info(f"[prepare_and_tariffs] android bridge cid={cid}, total: {total_ms:.0f}ms для {mac[:8]}***")
        tariff_url = f"/tariffs?{urlencode({'mac': mac, 'router_id': router_id, 'cid': cid})}"
        response = templates.TemplateResponse(
            "android_bridge.html",
            {
                "request": request,
                "mac": mac,
                "router_id": router_id,
                "cid": cid,
            },
        )
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["Refresh"] = f"0; url={tariff_url}"
        return response

    logger.info(f"[prepare_and_tariffs] redirect cid={cid} -> /tariffs, total: {total_ms:.0f}ms для {mac[:8]}***")
    tariff_url = f"/tariffs?{urlencode({'mac': mac, 'router_id': router_id, 'cid': cid})}"
    return RedirectResponse(url=tariff_url, status_code=303)


@app.get("/tariffs", response_class=HTMLResponse)
async def tariffs(request: Request, mac: str = "00:00:00:00:00:00", router_id: str = "astana_01", cid: str = ""):
    cid = (cid or make_cid())[:24]
    logger.info(f"[tariffs] cid={cid} mac={mac[:8]}*** router={router_id}")
    return _build_tariffs_response(request, mac, router_id, cid)


@app.post("/client_event")
async def client_event(request: Request):
    """Сервисный endpoint для диагностики клиентских шагов (loading, click, nav)."""
    try:
        data = await request.json()
    except Exception:
        return utf8_json_response({"ok": False, "error": "invalid json"}, status_code=400)

    cid = str(data.get("cid") or "-")[:24]
    stage = str(data.get("stage") or "-")[:64]
    page = str(data.get("page") or "-")[:64]
    elapsed_ms = data.get("elapsed_ms")
    extra = data.get("extra")
    logger.info(f"[CLIENT] cid={cid} page={page} stage={stage} elapsed_ms={elapsed_ms} extra={extra}")
    return utf8_json_response({"ok": True})


@app.get("/health")
async def health_check():
    """Диагностика всех роутеров"""
    logger.info("🩺 HEALTH CHECK: проверка всех роутеров...")
    health_report = {
        "timestamp": datetime.now(KZ_TZ).isoformat(),
        "routers": {}
    }
    
    for router_id, config in ROUTERS_CONFIG.items():
        logger.info(f"  Проверяю {router_id} ({config['ip']})...")
        status = {
            "ip": config['ip'],
            "connectivity": False,
            "hotspot_enabled": False,
            "details": ""
        }
        
        try:
            connection = routeros_api.RouterOsApiPool(
                config['ip'],
                username=config['user'],
                password=config['pass'],
                port=config.get('port', 8728),
                plaintext_login=True,
            )
            api = connection.get_api()
            
            # Проверяем связь
            identity = api.get_resource('/system/identity').call('print')
            if identity:
                status["connectivity"] = True
                status["details"] = f"Identity: {identity[0].get('name', 'N/A')}"
                logger.info(f"    ✓ Связь: OK ({status['details']})")
            
            # Проверяем hotspot
            hotspot_enabled = check_router_hotspot_enabled(config)
            status["hotspot_enabled"] = hotspot_enabled
            if hotspot_enabled:
                logger.info(f"    ✓ Hotspot: ENABLED")
            else:
                logger.warning(f"    ❌ Hotspot: DISABLED или ошибка конфигурации!")
                status["details"] += " | Hotspot: PROBLEM"
            
            connection.disconnect()
        except Exception as e:
            logger.error(f"    ❌ Ошибка подключения: {str(e)[:150]}")
            status["details"] = f"Error: {str(e)[:100]}"
        
        health_report["routers"][router_id] = status
    
    logger.info(f"🩺 HEALTH CHECK завершен")
    return utf8_json_response(health_report)


@app.get("/debug")
async def debug_router_status(mac: str = "00:00:00:00:00:00", router_id: str = "astana_01"):
    """Подробная диагностика конкретного MAC на роутере"""
    logger.info(f"🔍 DEBUG запрос: MAC={mac}, router={router_id}")
    
    config = ROUTERS_CONFIG.get(router_id)
    if not config:
        return utf8_json_response({"error": f"Неизвестный router_id: {router_id}"}, status_code=400)
    
    if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac):
        return utf8_json_response({"error": f"Некорректный MAC: {mac}"}, status_code=400)
    
    debug_info = {
        "mac": mac,
        "router_id": router_id,
        "router_ip": config['ip'],
        "bindings": [],
        "active_sessions": [],
        "users": [],
        "schedulers": []
    }
    
    try:
        connection = routeros_api.RouterOsApiPool(
            config['ip'],
            username=config['user'],
            password=config['pass'],
            port=config.get('port', 8728),
            plaintext_login=True,
        )
        api = connection.get_api()
        
        # Биндинги
        binding = api.get_resource('/ip/hotspot/ip-binding')
        bindings = binding.call('print', queries={'mac-address': mac})
        for b in bindings:
            debug_info["bindings"].append({
                "type": b.get('type'),
                "comment": b.get('comment'),
                "id": b.get('id')
            })
        
        # Активные сессии
        active = api.get_resource('/ip/hotspot/active')
        active_sessions = active.call('print', queries={'mac-address': mac})
        for a in active_sessions:
            debug_info["active_sessions"].append({
                "user": a.get('user'),
                "address": a.get('address'),
                "uptime": a.get('uptime'),
                "id": a.get('id')
            })
        
        # Юзеры с этим MAC
        user_res = api.get_resource('/ip/hotspot/user')
        users = user_res.call('print')
        for u in users:
            if mac in (u.get('comment', '') or ''):
                debug_info["users"].append({
                    "name": u.get('name'),
                    "comment": u.get('comment'),
                    "id": u.get('id')
                })
        
        # Schedulers для этого MAC
        sched = api.get_resource('/system/scheduler')
        task_name_pattern = mac.replace(':', '')
        all_schedulers = sched.call('print')
        for s in all_schedulers:
            name = s.get('name', '')
            if task_name_pattern in name:
                debug_info["schedulers"].append({
                    "name": name,
                    "start-date": s.get('start-date'),
                    "start-time": s.get('start-time'),
                    "comment": s.get('comment'),
                    "id": s.get('id')
                })
        
        connection.disconnect()
        logger.info(f"🔍 DEBUG: найдено {len(debug_info['bindings'])} биндингов, {len(debug_info['active_sessions'])} активных, {len(debug_info['schedulers'])} schedulers")
    except Exception as e:
        logger.error(f"🔍 DEBUG: ошибка: {str(e)[:150]}")
        return utf8_json_response({"error": str(e)}, status_code=500)
    
    return utf8_json_response(debug_info)


@app.get("/payment_methods", response_class=HTMLResponse)
@app.get("/payment_methods.html", response_class=HTMLResponse)
async def payment_methods_page(request: Request, mac: str = "00:00:00:00:00:00", router_id: str = "astana_01"):
    return templates.TemplateResponse("payment_methods.html", {"request": request, "mac": mac, "router_id": router_id})


@app.get("/offer", response_class=HTMLResponse)
@app.get("/offer.html", response_class=HTMLResponse)
async def offer_page(request: Request, mac: str = "00:00:00:00:00:00", router_id: str = "astana_01"):
    return templates.TemplateResponse("offer.html", {"request": request, "mac": mac, "router_id": router_id})


@app.get("/privacy", response_class=HTMLResponse)
@app.get("/privacy.html", response_class=HTMLResponse)
async def privacy_page(request: Request, mac: str = "00:00:00:00:00:00", router_id: str = "astana_01"):
    return templates.TemplateResponse("privacy.html", {"request": request, "mac": mac, "router_id": router_id})


@app.get("/choose_payment", response_class=HTMLResponse)
async def choose_payment(
    request: Request,
    amount: int,
    mac: str,
    router_id: str = "astana_01",
    cid: str = "",
):
    """Промежуточная страница выбора способа оплаты (Kaspi/FreedomPay)."""
    cid = (cid or "-")[:24]
    logger.info(f"[choose_payment] cid={cid} amount={amount} mac={mac[:8]}*** router={router_id}")

    _, _, amount_to_title, allowed_amounts = get_tariff_runtime_state()
    if amount not in allowed_amounts:
        return utf8_json_response({"error": "Некорректная сумма"}, status_code=400)

    if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac or ""):
        return utf8_json_response({"error": "Некорректный MAC-адрес"}, status_code=400)

    if router_id not in ROUTERS_CONFIG:
        logger.error(f"[choose_payment] Неизвестный router_id: {router_id}")
        return utf8_json_response({"error": "Неизвестный роутер"}, status_code=400)

    tariff_name = amount_to_title.get(amount, "")
    response = templates.TemplateResponse(
        "choose_payment.html",
        {
            "request": request,
            "amount": amount,
            "mac": mac,
            "router_id": router_id,
            "cid": cid,
            "tariff_name": tariff_name,
            "kaspi_enabled": KASPI_ENABLED,
        },
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.post("/api/kaspi/create_order")
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

    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO kaspi_orders (
                local_order_id,
                contract_number,
                external_order_ref,
                mac_address,
                router_id,
                amount,
                minutes,
                kaspi_status,
                is_activated,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                local_order_id,
                contract_number,
                contract_number,
                mac,
                router_id,
                amount,
                minutes,
                "CREATED",
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        return utf8_json_response({"error": "Конфликт заказа, повторите"}, status_code=409)
    finally:
        conn.close()

    logger.info("[KASPI] local order created cid=%s contract=%s mac=%s router=%s amount=%s", cid, contract_number, mac[:8] + "***", router_id, amount)

    return utf8_json_response(
        {
            "ok": True,
            "local_order_id": local_order_id,
            "contract_number": contract_number,
            "amount": amount,
            "minutes": minutes,
            "tariff_name": amount_to_title.get(amount, ""),
            "status_url": f"/api/kaspi/order_status?contract_number={contract_number}",
            "success_url": f"/success?mac={mac}&router_id={router_id}&minutes={minutes}&amount={amount}&payment_method=kaspi&contract_number={contract_number}&cid={cid}",
        }
    )


@app.get("/api/kaspi/order_status")
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


@app.post("/api/kaspi/sync_once")
async def kaspi_sync_trigger():
    result = await kaspi_sync_once()
    return utf8_json_response(result)


@app.get("/start_payment")
async def start_payment(request: Request, amount: int, mac: str, router_id: str = "astana_01", cid: str = ""):
    cid = (cid or "-")[:24]
    logger.info(f"[start_payment] START cid={cid} amount={amount} mac={mac[:8]}*** router={router_id}")
    if not MERCHANT_ID or not SECRET_KEY:
        logger.error("[start_payment] FreedomPay env is not configured")
        return utf8_json_response({"error": "Платежный шлюз временно недоступен"}, status_code=503)
    _, _, _, allowed_amounts = get_tariff_runtime_state()
    if amount not in allowed_amounts:
        return utf8_json_response({"error": "Некорректная сумма"}, status_code=400)
    
    if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac or ""):
        return utf8_json_response({"error": "Некорректный MAC-адрес"}, status_code=400)

    if router_id not in ROUTERS_CONFIG:
        logger.error(f"[start_payment] Неизвестный router_id: {router_id}")
        return utf8_json_response({"error": "Неизвестный роутер"}, status_code=400)

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


@app.get("/activate_welcome")
async def activate_welcome(request: Request, mac: str, router_id: str = "astana_01"):
    if not mac or not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac):
        logger.warning(f"[activate_welcome] Некорректный MAC: {mac}")
        return utf8_json_response({"error": "Некорректный MAC"}, status_code=400)

    if router_id not in ROUTERS_CONFIG:
        logger.error(f"[activate_welcome] Неизвестный router_id: {router_id}")
        return utf8_json_response({"error": "Неизвестный роутер"}, status_code=400)

    expires_at = (datetime.utcnow() + timedelta(seconds=180)).isoformat()
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO orders (mac_address, amount, status, router_id, expires_at) VALUES (?, 0, 'PAY_WINDOW', ?, ?)",
            (mac, router_id, expires_at),
        )
        conn.commit()
    finally:
        conn.close()

    # PAY_WINDOW создаётся синхронно через /api/prepare_access из welcome.html
    # activate_welcome теперь только создаёт запись в БД и редиректит
    tariff_url = f"/tariffs?{urlencode({'mac': mac, 'router_id': router_id})}"
    return RedirectResponse(url=tariff_url, status_code=302)


@app.post("/get_free_trial")
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
    return utf8_json_response(
        {"error": "Бесплатный доступ временно отключен"},
        status_code=403,
    )


@app.post("/payment_result")
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
        paid_expires_at = (datetime.utcnow() + timedelta(minutes=minutes)).isoformat()

        logger.info(f"[payment_result] Активирую PAID на {minutes} минут для {mac[:8]}*** на {router_id}")
        if not mac or not await asyncio.to_thread(set_mikrotik_ah_access, mac, router_id, minutes, mode="PAID"):
            logger.error(f"[payment_result] ❌ ОШИБКА: Не удалось активировать PAID для {mac[:8]}*** на {router_id}")
            return Response(content="Activation failed", status_code=500)

        conn = get_db()
        try:
            updated = 0
            if payment_order_id:
                updated = conn.execute(
                    "UPDATE orders SET status = 'PAID', amount = ?, mac_address = ?, router_id = ?, expires_at = ? WHERE payment_order_id = ?",
                    (amount, mac, router_id, paid_expires_at, payment_order_id),
                ).rowcount
            elif mac:
                updated = conn.execute(
                    "UPDATE orders SET status = 'PAID', amount = ?, router_id = ?, expires_at = ? WHERE id = (SELECT id FROM orders WHERE mac_address = ? AND status IN ('PAYMENT_INITIATED', 'PAY_WINDOW') ORDER BY id DESC LIMIT 1)",
                    (amount, router_id, paid_expires_at, mac),
                ).rowcount

            if not updated:
                conn.execute(
                    "INSERT INTO orders (mac_address, amount, status, router_id, payment_order_id, expires_at) VALUES (?, ?, 'PAID', ?, ?, ?)",
                    (mac, amount, router_id, payment_order_id, paid_expires_at),
                )
            conn.commit()
            logger.info(f"[payment_result] ✓ УСПЕХ: {amount} ₸ обработано для {mac[:8]}*** на {minutes} минут")
            logger.info(f"[payment_result] 🔍 Для диагностики: http://wifi-pay.kz/debug?mac={mac}&router_id={router_id}")
        finally:
            conn.close()

        return Response(content="OK", status_code=200)
    
    except Exception as e:
        logger.error(f"[payment_result] ❌ ОШИБКА обработки платежа: {str(e)[:200]}")
        return Response(content="Internal error", status_code=500)

@app.get("/success", response_class=HTMLResponse)
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
                """
                SELECT kaspi_status, is_activated
                FROM kaspi_orders
                WHERE contract_number = ?
                """,
                (contract_number,),
            ).fetchone()
            if row:
                kaspi_status = (row["kaspi_status"] or "")
                kaspi_is_activated = bool(row["is_activated"])
        finally:
            conn.close()

    return templates.TemplateResponse("success.html", {
        "request": request,
        "mac": mac,
        "router_id": router_id,
        "minutes": minutes,
        "amount": amount,
        "tariff_name": tariff_name,
        "payment_method": payment_method,
        "contract_number": contract_number,
        "kaspi_status": kaspi_status,
        "kaspi_is_activated": "true" if kaspi_is_activated else "false",
    })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)