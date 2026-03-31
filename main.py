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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlencode, unquote

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, Response
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

def utf8_json_response(content, status_code=200):
    return JSONResponse(content, status_code=status_code, headers={"Content-Type": "application/json; charset=utf-8"})

# --- ЗАГРУЗКА КОНФИГА РОУТЕРОВ ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROUTERS_CONFIG_PATH = os.path.join(BASE_DIR, "routers_config.json")

if os.path.exists(ROUTERS_CONFIG_PATH):
    with open(ROUTERS_CONFIG_PATH, encoding="utf-8") as f:
        routers_list = json.load(f)
    ROUTERS_CONFIG = {router["id"]: router for router in routers_list}
else:
    ROUTERS_CONFIG = {}

MERCHANT_ID = os.getenv("MERCHANT_ID", "581983")
SECRET_KEY = os.getenv("SECRET_KEY", "PMwioQEEEOFbDBAu")
PAY_URL = os.getenv("PAY_URL", "https://api.freedompay.kz/payment.php")
KZ_TZ = ZoneInfo("Asia/Almaty")
TRIAL_TOKEN_TTL_SECONDS = 5 * 60
TRIAL_RATE_LIMIT_WINDOW_SECONDS = 10 * 60
TRIAL_RATE_LIMIT_MAX_REQUESTS = 6
TRIAL_RATE_BUCKET = {}

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

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

        if access_mode != "ACTIVE":
            try:
                binding.call('add', arguments={'mac-address': mac, 'type': 'bypassed', 'comment': f"{mode}_{mac}"})
                logger.info(f"Fallback: биндинг BYPASS добавлен для {mac[:8]}***")
            except Exception as e:
                logger.error(f"❌ Ошибка fallback-биндинга для {mac}: {str(e)[:150]}")


def _mikrotik_setup_scheduler(api, sched, mac, user_name, mode, seconds, minutes):
    """Настраивает scheduler на MikroTik для автоочистки доступа."""
    try:
        clock_info = api.get_resource('/system/clock').call('print')[0]
        date_str = clock_info.get('date', '').title()
        time_str = clock_info.get('time', '')
        mt_now = datetime.strptime(f"{date_str} {time_str}", "%b/%d/%Y %H:%M:%S")
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

def set_mikrotik_ah_access(mac: str, router_id: str, minutes: int, mode: str, seconds: int | None = None):
    config = ROUTERS_CONFIG.get(router_id)
    if not config:
        logger.error(f"❌ Неизвестный router_id: {router_id}")
        return False

    if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac or ""):
        logger.error(f"❌ Некорректный MAC: {mac}")
        return False

    connection = None
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            connection = routeros_api.RouterOsApiPool(
                config['ip'],
                username=config['user'],
                password=config['pass'],
                port=config.get('port', 8728),
                plaintext_login=True,
            )
            api = connection.get_api()

            # Быстрый путь для PAY_WINDOW: только биндинг, без scheduler/cleanup юзеров/active
            if mode == 'PAY_WINDOW':
                binding = api.get_resource('/ip/hotspot/ip-binding')
                for b in binding.call('print', queries={'mac-address': mac}):
                    comment = (b.get('comment') or '')
                    if comment.startswith('PAID_') or comment.startswith('TRIAL_'):
                        logger.info(f"PAY_WINDOW: уже есть {comment} для {mac[:8]}***, пропускаем")
                        return True
                    try:
                        binding.call('remove', arguments={'.id': b.get('id') or b.get('.id')})
                    except Exception:
                        pass
                binding.call('add', arguments={'mac-address': mac, 'type': 'bypassed', 'comment': f"PAY_WINDOW_{mac}"})
                logger.info(f"✓ PAY_WINDOW биндинг добавлен для {mac[:8]}***")
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

def build_payment_url(amount: int, mac: str, router_id: str, payment_order_id: str) -> str:
    minutes = 1440
    success_url = (
        f"https://wifi-pay.kz/success"
        f"?mac={mac}"
        f"&router_id={router_id}"
        f"&minutes={minutes}"
        f"&amount={amount}"
    )
    params = {
        'pg_merchant_id': MERCHANT_ID, 'pg_amount': str(amount), 'pg_currency': 'KZT',
        'pg_description': f"Wi-Fi {mac}", 'pg_order_id': payment_order_id,
        'pg_salt': 'salt', 'pg_param1': mac, 'pg_param2': router_id,
        'pg_result_url': 'https://wifi-pay.kz/payment_result',
        'pg_success_url': success_url,
    }
    params['pg_sig'] = get_signature("payment.php", params, SECRET_KEY)
    return f"{PAY_URL}?{urlencode(params)}"

@app.get("/session_status")
async def session_status(mac: str, router_id: str = "astana_01"):
    """Возвращает статус сессии по MAC. Используется клиентом для поллинга."""
    if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac or ""):
        return utf8_json_response({"active": False, "expires_in": -1})
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT status, expires_at FROM orders
               WHERE mac_address=? AND router_id=? AND status IN ('PAY_WINDOW','TRIAL','PAID')
               ORDER BY created_at DESC LIMIT 1""",
            (mac, router_id)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return utf8_json_response({"active": False, "expires_in": -1})
    status, expires_at_str = row
    if status == 'PAID':
        return utf8_json_response({"active": True, "expires_in": 86400})
    if not expires_at_str:
        return utf8_json_response({"active": True, "expires_in": -1})
    try:
        expires_at = datetime.fromisoformat(expires_at_str)
        expires_in = int((expires_at - datetime.utcnow()).total_seconds())
        return utf8_json_response({"active": expires_in > 0, "expires_in": expires_in})
    except Exception:
        return utf8_json_response({"active": False, "expires_in": -1})

@app.get("/", response_class=HTMLResponse)
async def welcome(request: Request, mac: str = "00:00:00:00:00:00", router_id: str = "astana_01"):
    return templates.TemplateResponse("welcome.html", {"request": request, "mac": mac, "router_id": router_id})


@app.get("/tariffs", response_class=HTMLResponse)
async def tariffs(request: Request, mac: str = "00:00:00:00:00:00", router_id: str = "astana_01"):
    trial_ts = str(int(time.time()))
    trial_sig = make_trial_signature(mac, router_id, trial_ts)
    response = templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "mac": mac,
            "router_id": router_id,
            "trial_ts": trial_ts,
            "trial_sig": trial_sig,
        },
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


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


@app.get("/start_payment")
async def start_payment(request: Request, amount: int, mac: str, router_id: str = "astana_01"):
    if amount not in[100, 200, 490, 990, 2490]:
        return utf8_json_response({"error": "Некорректная сумма"}, status_code=400)
    
    if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac or ""):
        return utf8_json_response({"error": "Некорректный MAC-адрес"}, status_code=400)

    if router_id not in ROUTERS_CONFIG:
        logger.error(f"[start_payment] Неизвестный router_id: {router_id}")
        return utf8_json_response({"error": "Неизвестный роутер"}, status_code=400)

    logger.info(f"[start_payment] Активирую PAY_WINDOW на 3 мин для {mac[:8]}*** на {router_id}")
    if not await asyncio.to_thread(set_mikrotik_ah_access, mac, router_id, minutes=3, mode="PAY_WINDOW"):
        logger.error(f"[start_payment] ❌ ОШИБКА: PAY_WINDOW не активирован для {mac[:8]}*** на {router_id}")
        return utf8_json_response({"error": "Ошибка активации доступа"}, status_code=500)

    logger.info(f"[start_payment] ✓ PAY_WINDOW активирован, инициирую платеж на {amount} ₸")
    logger.info(f"[start_payment] 🔍 Для диагностики: http://wifi-pay.kz/debug?mac={mac}&router_id={router_id}")
    
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

    payment_url = build_payment_url(amount, mac, router_id, payment_order_id)
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

    # Запускаем PAY_WINDOW фоново — не ждём, редиректим сразу.
    asyncio.create_task(asyncio.to_thread(
        set_mikrotik_ah_access, mac, router_id, 3, "PAY_WINDOW"
    ))
    logger.info(f"[activate_welcome] PAY_WINDOW запущен фоново для {mac[:8]}*** ({router_id})")

    tariff_url = f"/tariffs?{urlencode({'mac': mac, 'router_id': router_id})}"
    return RedirectResponse(url=tariff_url, status_code=302)


@app.post("/get_free_trial")
async def get_free_trial(
    request: Request,
    mac: str = Form(...),
    router_id: str = Form(...),
    trial_ts: str = Form(""),
    trial_sig: str = Form(""),
):
    if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac or ""):
        return utf8_json_response({"error": "Некорректный MAC-адрес"}, status_code=400)

    if router_id not in ROUTERS_CONFIG:
        logger.error(f"[get_free_trial] Неизвестный router_id: {router_id}")
        return utf8_json_response({"error": "Неизвестный роутер"}, status_code=400)

    logger.info(f"[get_free_trial] Запрос TRIAL для {mac[:8]}*** на {router_id}")

    if is_trial_rate_limited(request):
        logger.warning(f"[get_free_trial] ⚠️ RATE LIMIT для IP {get_client_ip(request)}")
        return utf8_json_response({"error": "Слишком много попыток. Подождите 10 минут и повторите."}, status_code=429)

    if not is_valid_trial_signature(mac, router_id, trial_ts, trial_sig):
        logger.warning(f"[get_free_trial] ❌ Невалидная подпись для {mac[:8]}***")
        return utf8_json_response({"error": "Сессия истекла. Обновите страницу и попробуйте снова."}, status_code=400)

    device_id, is_new_device_id = get_or_create_device_id(request)

    if check_trial_used_last_24h(mac, device_id):
        logger.info(f"[get_free_trial] Trial уже использован за 24ч для {mac[:8]}*** (device_id: {device_id[:8]}***)")
        blocked = utf8_json_response({"error": "Бесплатный доступ уже использован. Повторно можно через 24 часа."}, status_code=403)
        if is_new_device_id:
            blocked.set_cookie(
                key="wf_device_id",
                value=device_id,
                max_age=60 * 60 * 24 * 365,
                httponly=True,
                secure=True,
                samesite="lax",
                path="/",
            )
        return blocked

    logger.info(f"[get_free_trial] ✓ Все проверки пройдены, активирую TRIAL на 15 минут для {mac[:8]}***")
    if not await asyncio.to_thread(set_mikrotik_ah_access, mac, router_id, minutes=15, mode="TRIAL"):
        logger.error(f"[get_free_trial] ❌ Ошибка активации TRIAL для {mac[:8]}*** на {router_id}")
        return utf8_json_response({"error": "Ошибка активации доступа"}, status_code=500)
    
    logger.info(f"[get_free_trial] ✓ TRIAL активирован на 15 минут для {mac[:8]}***")
    logger.info(f"[get_free_trial] 🔍 Для диагностики: http://wifi-pay.kz/debug?mac={mac}&router_id={router_id}")
    
    trial_expires_at = (datetime.utcnow() + timedelta(minutes=15)).isoformat()
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO orders (mac_address, amount, status, router_id, device_id, expires_at) VALUES (?, 0, 'TRIAL', ?, ?, ?)",
            (mac, router_id, device_id, trial_expires_at),
        )
        conn.commit()
    finally:
        conn.close()

    response = JSONResponse({"message": "15 минут активировано! Нажмите 'Готово' и пользуйтесь."})
    if is_new_device_id:
        response.set_cookie(
            key="wf_device_id",
            value=device_id,
            max_age=60 * 60 * 24 * 365,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
    return response


@app.post("/payment_result")
async def payment_result(request: Request):
    try:
        form_data = await request.form()
        params = dict(form_data)

        logger.info(f"[payment_result] Callback от FreedomPay: order_id={params.get('pg_order_id')}, amount={params.get('pg_amount')} ₸")

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
        minutes = 1440

        logger.info(f"[payment_result] Активирую PAID на {minutes} минут для {mac[:8]}*** на {router_id}")
        if not mac or not await asyncio.to_thread(set_mikrotik_ah_access, mac, router_id, minutes, mode="PAID"):
            logger.error(f"[payment_result] ❌ ОШИБКА: Не удалось активировать PAID для {mac[:8]}*** на {router_id}")
            return Response(content="Activation failed", status_code=500)

        conn = get_db()
        try:
            updated = 0
            if payment_order_id:
                updated = conn.execute(
                    "UPDATE orders SET status = 'PAID', amount = ?, mac_address = ?, router_id = ? WHERE payment_order_id = ?",
                    (amount, mac, router_id, payment_order_id),
                ).rowcount
            elif mac:
                updated = conn.execute(
                    "UPDATE orders SET status = 'PAID', amount = ?, router_id = ? WHERE id = (SELECT id FROM orders WHERE mac_address = ? AND status IN ('PAYMENT_INITIATED', 'PAY_WINDOW') ORDER BY id DESC LIMIT 1)",
                    (amount, router_id, mac),
                ).rowcount

            if not updated:
                conn.execute(
                    "INSERT INTO orders (mac_address, amount, status, router_id, payment_order_id) VALUES (?, ?, 'PAID', ?, ?)",
                    (mac, amount, router_id, payment_order_id),
                )
            conn.commit()
            logger.info(f"[payment_result] ✓ УСПЕХ: {amount} ₸ обработано для {mac[:8]}*** на 24 часа ({minutes} минут)")
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
):
    mac = decode_nested_url_value(mac)
    router_id = decode_nested_url_value(router_id)
    return templates.TemplateResponse("success.html", {
        "request": request,
        "mac": mac,
        "router_id": router_id,
        "minutes": minutes,
        "amount": amount,
    })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)