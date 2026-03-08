import os
import hashlib
import time
import logging
import sqlite3
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

from fastapi import FastAPI, Request, Form, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import routeros_api

# --- НАСТРОЙКИ ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("WiFiGateway")

app = FastAPI(title="Wi-Fi Gateway Final")

ROUTERS_CONFIG = {
    "astana_01": {
        "ip": "10.0.0.2",
        "user": "admin",
        "pass": "kaspiwifiadmin2026",
        "portal_probe_url": "http://captive.apple.com/hotspot-detect.html",
    }
}

MERCHANT_ID = "581983"
SECRET_KEY = "PMwioQEEEOFbDBAu"
PAY_URL = "https://api.freedompay.kz/payment.php"
KZ_TZ = ZoneInfo("Asia/Almaty")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

# --- БАЗА ДАННЫХ ---

def init_db():
    conn = sqlite3.connect(os.path.join(BASE_DIR, 'gateway.db'))
    conn.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mac_address TEXT,
            amount INTEGER,
            status TEXT,
            router_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


def check_trial_used_today(mac: str):
    conn = sqlite3.connect(os.path.join(BASE_DIR, 'gateway.db'))
    cursor = conn.cursor()
    today_start = datetime.now(KZ_TZ).strftime("%Y-%m-%d 00:00:00")
    cursor.execute(
        "SELECT id FROM orders WHERE mac_address = ? AND status = 'TRIAL' AND created_at >= ?",
        (mac, today_start),
    )
    res = cursor.fetchone()
    conn.close()
    return res is not None


init_db()


# --- ЯДРО: MIKROTIK API (СТАТУС A H) ---


def set_mikrotik_ah_access(mac: str, router_id: str, minutes: int, mode: str):
    config = ROUTERS_CONFIG.get(router_id)
    if not config:
        logger.error(f"❌ Неизвестный router_id: {router_id}")
        return False

    if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac or ""):
        logger.error(f"❌ Некорректный MAC: {mac}")
        return False

    connection = None
    try:
        connection = routeros_api.RouterOsApiPool(
            config['ip'],
            username=config['user'],
            password=config['pass'],
            port=config.get('port', 8728),
            plaintext_login=True,
        )
        api = connection.get_api()

        binding = api.get_resource('/ip/hotspot/ip-binding')
        active = api.get_resource('/ip/hotspot/active')
        user_res = api.get_resource('/ip/hotspot/user')
        host_res = api.get_resource('/ip/hotspot/host')
        sched = api.get_resource('/system/scheduler')

        user_name = f"T-{mac.replace(':', '')}"
        user_pass = f"p{int(time.time()) % 1000000}"

        for b in binding.call('print', queries={'mac-address': mac}):
            binding.call('remove', arguments={'.id': b.get('id') or b.get('.id')})
        for a in active.call('print', queries={'mac-address': mac}):
            active.call('remove', arguments={'.id': a.get('id') or a.get('.id')})
        for u in user_res.call('print', queries={'name': user_name}):
            user_res.call('remove', arguments={'.id': u.get('id') or u.get('.id')})

        user_res.call('add', arguments={
            'name': user_name,
            'password': user_pass,
            'limit-uptime': f"{minutes}m",
            'comment': f"{mode}_{mac}",
        })

        host_ip = None
        host_server = None
        hosts = host_res.call('print', queries={'mac-address': mac})
        if hosts:
            host_ip = hosts[0].get('address')
            host_server = hosts[0].get('server')

        if host_ip:
            login_args = {
                'user': user_name,
                'password': user_pass,
                'mac-address': mac,
                'ip': host_ip,
            }
            if host_server:
                login_args['server'] = host_server
            try:
                active.call('login', arguments=login_args)
                active_rows = active.call('print', queries={'mac-address': mac})
                if active_rows:
                    logger.info(f"✅ Active login выполнен для {mac} ({host_ip})")
                else:
                    raise RuntimeError("login command finished but active session not found")
            except Exception as login_error:
                logger.warning(f"⚠️ Active login не выполнен для {mac}: {login_error}; включаем bypass fallback")
                binding.call('add', arguments={'mac-address': mac, 'type': 'bypassed', 'comment': f"{mode}_{mac}"})
        else:
            logger.warning(f"⚠️ Host для MAC {mac} не найден, включаем bypass fallback")
            binding.call('add', arguments={'mac-address': mac, 'type': 'bypassed', 'comment': f"{mode}_{mac}"})

        now = datetime.now(KZ_TZ)
        expiry = now + timedelta(minutes=minutes)
        mt_date = expiry.strftime("%b/%d/%Y").lower()
        mt_time = expiry.strftime("%H:%M:%S")
        task_name = f"del_{mac.replace(':', '')}"

        for t in sched.call('print', queries={'name': task_name}):
            sched.call('remove', arguments={'.id': t.get('id') or t.get('.id')})

        on_event = (
            f'/ip hotspot ip-binding remove [find mac-address="{mac}"]; '
            f'/ip hotspot user remove [find name="{user_name}"]; '
            f'/system scheduler remove [find name="{task_name}"];'
        )
        sched.call('add', arguments={
            'name': task_name,
            'start-date': mt_date,
            'start-time': mt_time,
            'interval': "00:00:00",
            'on-event': on_event,
            'comment': f"AUTOCLEAR_{mode}_{mac}",
        })

        logger.info(f"✅ Статус A H ({mode}) активирован на {minutes} мин для {mac}")
        return True
    except Exception as e:
        logger.exception(f"❌ Ошибка API MikroTik ({router_id}/{config.get('ip')}): {e}")
        return False
    finally:
        if connection:
            connection.disconnect()


# --- МАРШРУТЫ ---

def get_signature(script_name, params, secret_key):
    sorted_keys = sorted(params.keys())
    values = [str(params[k]) for k in sorted_keys if k != 'pg_sig' and params[k] is not None]
    sig_str = f"{script_name};{';'.join(values)};{secret_key}"
    return hashlib.md5(sig_str.encode('utf-8')).hexdigest()

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, mac: str = "00:00:00:00:00:00", router_id: str = "astana_01"):
    return templates.TemplateResponse("index.html", {"request": request, "mac": mac, "router_id": router_id})


@app.post("/get_free_trial")
async def get_free_trial(mac: str = Form(...), router_id: str = Form(...)):
    """Выдача 15 минут один раз в сутки"""
    if check_trial_used_today(mac):
        return JSONResponse({"error": "Бесплатный доступ уже использован сегодня. Ждем вас завтра!"}, status_code=403)

    if set_mikrotik_ah_access(mac, router_id, minutes=15, mode="TRIAL"):
        conn = sqlite3.connect(os.path.join(BASE_DIR, 'gateway.db'))
        conn.execute(
            "INSERT INTO orders (mac_address, amount, status, router_id) VALUES (?, 0, 'TRIAL', ?)",
            (mac, router_id),
        )
        conn.commit()
        conn.close()
        return {
            "message": "15 минут активировано! Нажмите 'Готово' и пользуйтесь.",
            "close_url": ROUTERS_CONFIG.get(router_id, {}).get("portal_probe_url", "http://captive.apple.com/hotspot-detect.html"),
        }
    return JSONResponse({"error": "Ошибка роутера"}, status_code=500)


@app.post("/get_pay_link")
async def get_pay_link(amount: int = Form(...), mac: str = Form(...), router_id: str = Form(...)):
    # ШАГ 1: Окно для оплаты - строго 3 минуты (Статус A H)
    if not set_mikrotik_ah_access(mac, router_id, minutes=3, mode="PAY_WINDOW"):
        return JSONResponse({"error": "Роутер недоступен или MAC некорректен"}, status_code=500)

    # ШАГ 2: Ссылка в банк
    params = {
        'pg_merchant_id': MERCHANT_ID, 'pg_amount': str(amount), 'pg_currency': 'KZT',
        'pg_description': f"Wi-Fi {mac}", 'pg_order_id': str(int(time.time())),
        'pg_salt': 'salt', 'pg_param1': mac, 'pg_param2': router_id,
        'pg_result_url': 'https://wifi-pay.kz/payment_result', 'pg_success_url': 'https://wifi-pay.kz/success'
    }
    params['pg_sig'] = get_signature("payment.php", params, SECRET_KEY)
    return {"url": f"{PAY_URL}?{urlencode(params)}"}


@app.post("/payment_result")
async def payment_result(request: Request):
    form_data = await request.form()
    params = dict(form_data)

    if params.get('pg_sig') == get_signature("payment_result", params, SECRET_KEY) and params.get('pg_result') == '1':
        mac, router_id = params.get('pg_param1'), params.get('pg_param2', 'astana_01')
        amount = int(float(params.get('pg_amount', 0)))

        minutes = 60 if amount < 900 else 180 if amount < 2000 else 1440

        if mac and set_mikrotik_ah_access(mac, router_id, minutes, mode="PAID"):
            conn = sqlite3.connect(os.path.join(BASE_DIR, 'gateway.db'))
            conn.execute(
                "INSERT INTO orders (mac_address, amount, status, router_id) VALUES (?, ?, 'PAID', ?)",
                (mac, amount, router_id),
            )
            conn.commit()
            conn.close()
            return Response(content="Accepted", status_code=200)

    return Response(content="Error", status_code=400)

@app.get("/success", response_class=HTMLResponse)
async def success():
    return "<html><body style='text-align:center;padding-top:50px;'><h1>Оплата принята!</h1><p>Интернет активирован. Приятного пользования!</p></body></html>"

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)