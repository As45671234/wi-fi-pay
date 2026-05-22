"""
app/utils.py — Pure helpers: MAC, HMAC, cookies, tariffs, rate-limiting, QR/trial sigs.
"""

import os
import re
import hmac
import time
import hashlib
import base64
import secrets
import json

from fastapi import Request
from fastapi.responses import JSONResponse

from .config import (
    SECRET_KEY, ROUTERS_CONFIG, QR_TOKEN_SECRET, TARIFFS_CONFIG_PATH,
    TRIAL_TOKEN_TTL_SECONDS, TRIAL_RATE_LIMIT_WINDOW_SECONDS, TRIAL_RATE_LIMIT_MAX_REQUESTS,
    QR_TOKEN_TTL_SECONDS, QR_FALLBACK_MAX_IDLE_SECONDS,
    DEVICE_COOKIE_NAME, DEVICE_COOKIE_TTL_DAYS,
    logger,
)
from .db import get_db


# ── JSON response helper ───────────────────────────────────────────────────

def utf8_json_response(content, status_code=200):
    return JSONResponse(content, status_code=status_code, headers={"Content-Type": "application/json; charset=utf-8"})


def make_cid() -> str:
    return secrets.token_hex(6)


def get_client_ip(request: Request) -> str:
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        return xff.split(",")[0].strip()
    client = request.client
    return (client.host if client else "unknown") or "unknown"


# ── MAC helpers ────────────────────────────────────────────────────────────

def _normalize_mac(mac: str) -> str:
    return (mac or "").upper()


def _is_valid_mac(mac: str) -> bool:
    mac_norm = _normalize_mac(mac)
    if not re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", mac_norm):
        return False
    return mac_norm != "00:00:00:00:00:00"


# ── RouterOS duration parser ───────────────────────────────────────────────

def _routeros_duration_to_seconds(raw: str) -> int | None:
    value = (raw or "").strip().lower()
    if not value:
        return None

    if re.fullmatch(r"\d+", value):
        return int(value)

    if re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", value):
        hh, mm, ss = value.split(":")
        return int(hh) * 3600 + int(mm) * 60 + int(ss)

    m = re.fullmatch(r"(\d+)d(\d{1,2}):(\d{2}):(\d{2})", value)
    if m:
        days, hh, mm, ss = m.groups()
        return int(days) * 86400 + int(hh) * 3600 + int(mm) * 60 + int(ss)

    total = 0
    parsed_any = False
    for amount, unit in re.findall(r"(\d+)([wdhms])", value):
        parsed_any = True
        n = int(amount)
        if unit == "w":
            total += n * 604800
        elif unit == "d":
            total += n * 86400
        elif unit == "h":
            total += n * 3600
        elif unit == "m":
            total += n * 60
        elif unit == "s":
            total += n
    return total if parsed_any else None


# ── Tariff helpers ─────────────────────────────────────────────────────────

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
    return {"amount": amount, "minutes": minutes, "title": title, "subtitle": subtitle, "badge": badge}


_tariffs_cache: list[dict] | None = None
_tariffs_cache_ts: float = 0.0
_TARIFFS_CACHE_TTL = 60.0


def load_tariffs_config() -> list[dict]:
    global _tariffs_cache, _tariffs_cache_ts
    now = time.monotonic()
    if _tariffs_cache is not None and (now - _tariffs_cache_ts) < _TARIFFS_CACHE_TTL:
        return _tariffs_cache
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
    seen_amounts: set = set()
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
        normalized = [t for t in (_normalize_tariff(x) for x in default) if t is not None]
    normalized.sort(key=lambda x: x["amount"])
    _tariffs_cache = normalized
    _tariffs_cache_ts = time.monotonic()
    return normalized


def get_tariff_runtime_state() -> tuple[list[dict], dict[int, int], dict[int, str], list[int]]:
    tariffs = load_tariffs_config()
    amount_to_minutes = {t["amount"]: t["minutes"] for t in tariffs}
    amount_to_title = {t["amount"]: t["title"] for t in tariffs}
    allowed_amounts = sorted(amount_to_minutes.keys())
    return tariffs, amount_to_minutes, amount_to_title, allowed_amounts


# ── Device / trial helpers ─────────────────────────────────────────────────

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
            """,
            (mac, device_id),
        )
        return cursor.fetchone() is not None
    finally:
        conn.close()


# ── Rate limiting ──────────────────────────────────────────────────────────

TRIAL_RATE_BUCKET: dict = {}


def is_trial_rate_limited(request: Request) -> bool:
    ip = get_client_ip(request)
    now = int(time.time())
    if len(TRIAL_RATE_BUCKET) > 2000:
        cutoff = now - TRIAL_RATE_LIMIT_WINDOW_SECONDS
        stale = [k for k, v in TRIAL_RATE_BUCKET.items() if not v or max(v) < cutoff]
        for k in stale:
            del TRIAL_RATE_BUCKET[k]
    recent = [ts for ts in TRIAL_RATE_BUCKET.get(ip, []) if now - ts < TRIAL_RATE_LIMIT_WINDOW_SECONDS]
    if len(recent) >= TRIAL_RATE_LIMIT_MAX_REQUESTS:
        TRIAL_RATE_BUCKET[ip] = recent
        return True
    recent.append(now)
    TRIAL_RATE_BUCKET[ip] = recent
    return False


# ── Trial HMAC signatures ──────────────────────────────────────────────────

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


# ── QR HMAC signatures ─────────────────────────────────────────────────────

def make_router_qr_signature(router_id: str, ts: str) -> str:
    payload = f"{router_id}|{ts}"
    return hmac.new(QR_TOKEN_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def is_valid_router_qr_signature(router_id: str, ts: str, sig: str) -> bool:
    if not router_id or router_id not in ROUTERS_CONFIG:
        return False
    if not ts or not re.fullmatch(r"\d{10}", ts):
        return False
    if not sig or not re.fullmatch(r"[0-9a-fA-F]{64}", sig):
        return False
    now = int(time.time())
    if abs(now - int(ts)) > QR_TOKEN_TTL_SECONDS:
        return False
    expected = make_router_qr_signature(router_id, ts)
    return hmac.compare_digest(expected, sig.lower())


# ── Device cookie ──────────────────────────────────────────────────────────

def _make_device_cookie(mac: str, router_id: str) -> str:
    payload = f"{mac}|{router_id}"
    sig = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    encoded = base64.urlsafe_b64encode(payload.encode()).decode()
    return f"{encoded}.{sig}"


def _parse_device_cookie(cookie_value: str) -> tuple[str, str] | None:
    if not cookie_value:
        return None
    try:
        parts = cookie_value.split(".")
        if len(parts) != 2:
            return None
        encoded, sig = parts
        payload = base64.urlsafe_b64decode(encoded.encode()).decode()
        expected_sig = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expected_sig):
            return None
        mac, router_id = payload.split("|", 1)
        if not _is_valid_mac(_normalize_mac(mac)):
            return None
        if router_id not in ROUTERS_CONFIG:
            return None
        return _normalize_mac(mac), router_id
    except Exception:
        return None


def _set_device_cookie(response, mac: str, router_id: str) -> None:
    value = _make_device_cookie(mac, router_id)
    response.set_cookie(
        key=DEVICE_COOKIE_NAME,
        value=value,
        max_age=DEVICE_COOKIE_TTL_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=True,
    )


def _get_mac_from_cookie(request: Request) -> tuple[str, str] | None:
    cookie = request.cookies.get(DEVICE_COOKIE_NAME, "")
    return _parse_device_cookie(cookie)
