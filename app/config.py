"""
app/config.py — All settings, constants, and global state.
"""

import os
import json
import time
import logging
import asyncio
import threading
import concurrent.futures
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi.templating import Jinja2Templates

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("WiFiGateway")

# ── Paths ──────────────────────────────────────────────────────────────────
# BASE_DIR = app/, PROJECT_DIR = project root
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)

ROUTERS_CONFIG_PATH = os.path.join(PROJECT_DIR, "routers_config.json")
TARIFFS_CONFIG_PATH = os.path.join(PROJECT_DIR, "tariffs_config.json")
DB_PATH = os.path.join(PROJECT_DIR, "gateway.db")

STATIC_DIR = os.path.join(PROJECT_DIR, "static")
IMG_DIR = os.path.join(PROJECT_DIR, "img")

# ── Router config ──────────────────────────────────────────────────────────
ROUTER_USER_ENV = os.getenv("ROUTER_USER", "").strip()
ROUTER_PASS_ENV = os.getenv("ROUTER_PASS", "").strip()

if os.path.exists(ROUTERS_CONFIG_PATH):
    with open(ROUTERS_CONFIG_PATH, encoding="utf-8-sig") as _f:
        _routers_list = json.load(_f)
    ROUTERS_CONFIG = {r["id"]: r for r in _routers_list}
    for _r in ROUTERS_CONFIG.values():
        if ROUTER_USER_ENV:
            _r["user"] = ROUTER_USER_ENV
        if ROUTER_PASS_ENV:
            _r["pass"] = ROUTER_PASS_ENV
else:
    ROUTERS_CONFIG = {}

# ── Templates ──────────────────────────────────────────────────────────────
templates = Jinja2Templates(directory=os.path.join(PROJECT_DIR, "templates"))

# ── Payment gateway ────────────────────────────────────────────────────────
MERCHANT_ID = (os.getenv("MERCHANT_ID") or os.getenv("FREEDOMPAY_MERCHANT_ID") or "").strip()
SECRET_KEY = (os.getenv("SECRET_KEY") or os.getenv("FREEDOMPAY_SECRET_KEY") or "").strip()
PAY_URL = (os.getenv("PAY_URL") or os.getenv("FREEDOMPAY_API_URL") or "https://api.freedompay.kz/payment.php").strip()
BASE_URL = os.getenv("BASE_URL", "https://wifi-pay.kz").strip().rstrip("/")

if not MERCHANT_ID:
    logger.critical("MERCHANT_ID не задан в .env! Платежи FreedomPay не будут работать.")
if not SECRET_KEY:
    logger.critical("SECRET_KEY не задан в .env! Подписи и HMAC-защита нарушены — сервер не запустится безопасно.")
    raise RuntimeError("SECRET_KEY is required")

# ── Misc constants ─────────────────────────────────────────────────────────
KZ_TZ = ZoneInfo("Asia/Almaty")
ADMIN_TOKEN = (os.getenv("ADMIN_TOKEN") or "").strip()
DRIVER_ACCESS_PASSWORD = (os.getenv("DRIVER_ACCESS_PASSWORD") or "admin1234").strip()

TRIAL_TOKEN_TTL_SECONDS = 5 * 60
TRIAL_RATE_LIMIT_WINDOW_SECONDS = 10 * 60
TRIAL_RATE_LIMIT_MAX_REQUESTS = 6

PREPARE_TIMEOUT_SECONDS = 10

# ── Dedicated MikroTik thread pool ─────────────────────────────────────────
# 20 threads so 10+ simultaneous users never exhaust the pool.
# Each MikroTik call blocks ≤8 s (socket timeout), so 20 threads handle
# at least 20 concurrent calls before any queuing occurs.
MIKROTIK_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=20,
    thread_name_prefix="mikrotik",
)

# ── QR flow ────────────────────────────────────────────────────────────────
QR_TOKEN_TTL_SECONDS = max(60, int(os.getenv("QR_TOKEN_TTL_SECONDS", str(60 * 60 * 24 * 365)) or str(60 * 60 * 24 * 365)))
QR_TOKEN_SECRET = os.getenv("QR_TOKEN_SECRET") or SECRET_KEY
QR_CLIENT_AUTO_RETRY_MS = max(700, int(os.getenv("QR_CLIENT_AUTO_RETRY_MS", "1200") or "1200"))
QR_CLIENT_FIRST_RETRY_MS = max(200, int(os.getenv("QR_CLIENT_FIRST_RETRY_MS", "350") or "350"))
QR_FALLBACK_MAX_IDLE_SECONDS = max(5, int(os.getenv("QR_FALLBACK_MAX_IDLE_SECONDS", "15")))
QR_FALLBACK_POLL_TIMEOUT_Q_SECONDS = max(
    0.8,
    float(os.getenv("QR_FALLBACK_POLL_TIMEOUT_Q_SECONDS", os.getenv("QR_FALLBACK_POLL_TIMEOUT_SECONDS", "1.2"))),
)
QR_FALLBACK_POLL_TIMEOUT_AUTO_SECONDS = max(
    1.2,
    float(os.getenv("QR_FALLBACK_POLL_TIMEOUT_AUTO_SECONDS", os.getenv("QR_FALLBACK_POLL_TIMEOUT_SECONDS", "2.8"))),
)

# ── Cookie ─────────────────────────────────────────────────────────────────
DEVICE_COOKIE_NAME = "wf_dev"
DEVICE_COOKIE_TTL_DAYS = 30

# ── Pending activation ─────────────────────────────────────────────────────
PENDING_ACTIVATION_LOOP_INTERVAL_SECONDS = max(2, int(os.getenv("PENDING_ACTIVATION_LOOP_INTERVAL_SECONDS", "5") or "5"))
PENDING_ACTIVATION_RETRY_DELAY_SECONDS = max(5, int(os.getenv("PENDING_ACTIVATION_RETRY_DELAY_SECONDS", "20") or "20"))
PENDING_ACTIVATION_MAX_ATTEMPTS = max(1, int(os.getenv("PENDING_ACTIVATION_MAX_ATTEMPTS", "5") or "5"))

# ── Kaspi ──────────────────────────────────────────────────────────────────
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

# ── Global async / thread state ────────────────────────────────────────────
# These are mutated by startup/shutdown handlers in app/main.py.
kaspi_sync_task: asyncio.Task | None = None
kaspi_sync_stop = asyncio.Event()
kaspi_sync_lock = asyncio.Lock()
kaspi_activation_db_lock = threading.Lock()
pending_activation_task: asyncio.Task | None = None
pending_activation_stop = asyncio.Event()
