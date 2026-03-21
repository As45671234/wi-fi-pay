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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import routeros_api

# --- НАСТРОЙКИ ---
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
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
            payment_order_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(orders)")
    columns = {row[1] for row in cursor.fetchall()}
    if 'payment_order_id' not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN payment_order_id TEXT")
    conn.commit()
    conn.close()


def get_db_connection():
    """Context manager для безопасной работы с БД"""
    conn = sqlite3.connect(os.path.join(BASE_DIR, 'gateway.db'))
    try:
        yield conn
    finally:
        conn.close()


def check_trial_used_today(mac: str) -> bool:
    """Проверяет, использовал ли MAC пробный период сегодня"""
    conn = sqlite3.connect(os.path.join(BASE_DIR, 'gateway.db'))
    try:
        cursor = conn.cursor()
        today_start = datetime.now(KZ_TZ).strftime("%Y-%m-%d 00:00:00")
        cursor.execute(
            "SELECT id FROM orders WHERE mac_address = ? AND status = 'TRIAL' AND created_at >= ?",
            (mac, today_start),
        )
        return cursor.fetchone() is not None
    finally:
        conn.close()


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
        hosts = host_res.call('print', queries={'mac-address': mac})
        if hosts:
            host_ip = hosts[0].get('address')

        access_mode = "BYPASS"

        if host_ip:
            login_args = {'user': user_name, 'password': user_pass, 'mac-address': mac, 'ip': host_ip}
            try:
                active.call('login', arguments=login_args)
                active_rows = active.call('print', queries={'mac-address': mac})
                if active_rows:
                    access_mode = "ACTIVE"
                else:
                    raise RuntimeError("login command finished but active session not found")
            except Exception:
                binding.call('add', arguments={'mac-address': mac, 'type': 'bypassed', 'comment': f"{mode}_{mac}"})
        else:
            binding.call('add', arguments={'mac-address': mac, 'type': 'bypassed', 'comment': f"{mode}_{mac}"})

        now = datetime.now(KZ_TZ)
        expiry = now + timedelta(minutes=minutes)
        mt_date = expiry.strftime("%b/%d/%Y").lower()
        mt_time = expiry.strftime("%H:%M:%S")
        task_name = f"del_{mac.replace(':', '')}"

        for t in sched.call('print', queries={'name': task_name}):
            sched.call('remove', arguments={'.id': t.get('id') or t.get('.id')})

        on_event = (
            f'/ip hotspot active remove [find mac-address="{mac}"]; '
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

        if mode in ['PAID', 'PAY_WINDOW']:
            logger.info(f"Access granted: {mode} {minutes}min for {mac[:8]}***")
        return True
    except Exception as e:
        logger.error(f"MikroTik API error for {router_id}: {str(e)[:100]}")
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


def build_payment_url(amount: int, mac: str, router_id: str, payment_order_id: str) -> str:
    if amount >= 2000:
        minutes = 1440
    elif amount >= 900:
        minutes = 180
    elif amount >= 400:
        minutes = 60
    else:
        minutes = 5  # тест 100₸
    success_url = (
        f"https://wifi-pay.kz/success?"
        + urlencode({'mac': mac, 'router_id': router_id, 'minutes': minutes, 'amount': amount})
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

@app.get("/", response_class=HTMLResponse)
async def welcome(request: Request, mac: str = "00:00:00:00:00:00", router_id: str = "astana_01"):
    return templates.TemplateResponse("welcome.html", {"request": request, "mac": mac, "router_id": router_id})


@app.get("/tariffs", response_class=HTMLResponse)
async def tariffs(request: Request, mac: str = "00:00:00:00:00:00", router_id: str = "astana_01"):
    response = templates.TemplateResponse("index.html", {"request": request, "mac": mac, "router_id": router_id})
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


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
async def start_payment(amount: int, mac: str, router_id: str = "astana_01"):
    """Активирует окно оплаты и редиректит на FreedomPay"""
    # Валидация
    if amount not in [100, 490, 990, 2490]:  # 100 = тест 5мин
        return JSONResponse({"error": "Некорректная сумма"}, status_code=400)
    
    if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac or ""):
        return JSONResponse({"error": "Некорректный MAC-адрес"}, status_code=400)
    
    if not set_mikrotik_ah_access(mac, router_id, minutes=3, mode="PAY_WINDOW"):
        return JSONResponse({"error": "Ошибка активации доступа"}, status_code=500)

    payment_order_id = str(int(time.time() * 1000))
    conn = sqlite3.connect(os.path.join(BASE_DIR, 'gateway.db'))
    try:
        conn.execute(
            "INSERT INTO orders (mac_address, amount, status, router_id, payment_order_id) VALUES (?, ?, 'PAYMENT_INITIATED', ?, ?)",
            (mac, amount, router_id, payment_order_id),
        )
        conn.commit()
    finally:
        conn.close()

    payment_url = build_payment_url(amount, mac, router_id, payment_order_id)
    safe_payment_url = payment_url.replace("&", "&amp;")
    html = f"""
    <!DOCTYPE html>
    <html lang=\"ru\">
    <head>
        <meta charset=\"UTF-8\" />
        <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
        <title>Подготовка оплаты</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background:#f8fafc; display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }}
            .card {{ width:min(420px, 92vw); background:#fff; border:1px solid #e5e7eb; border-radius:20px; padding:24px; text-align:center; }}
            .title {{ margin:0 0 8px; font-size:20px; font-weight:700; color:#111827; }}
            .text {{ margin:0 0 16px; color:#6b7280; font-size:14px; line-height:1.4; }}
            .bar {{ height:8px; border-radius:999px; overflow:hidden; background:#e5e7eb; margin-bottom:16px; }}
            .bar > div {{ height:100%; width:0; background:#2563eb; animation:fill 1.6s linear forwards; }}
            .btn {{ display:block; text-decoration:none; background:#1c75ff; color:#fff; padding:12px 14px; border-radius:12px; font-weight:700; }}
            @keyframes fill {{ from {{ width:0; }} to {{ width:100%; }} }}
        </style>
        <script>
            setTimeout(function() {{ window.location.replace('{payment_url}'); }}, 1600);
        </script>
    </head>
    <body>
        <div class=\"card\">
            <h1 class=\"title\">Готовим оплату</h1>
            <p class=\"text\">Подождите 1-2 секунды, настраиваем доступ к платежной странице.</p>
            <div class=\"bar\"><div></div></div>
            <a class=\"btn\" href=\"{safe_payment_url}\">Открыть оплату сейчас</a>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html, status_code=200)


@app.get("/activate_welcome")
async def activate_welcome(request: Request, mac: str, router_id: str = "astana_01"):
    """Welcome step: Android without grant, iOS/others with short PAY_WINDOW grant"""
    if not mac or not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac):
        return JSONResponse({"error": "Некорректный MAC"}, status_code=400)

    user_agent = (request.headers.get("user-agent") or "").lower()
    is_android = "android" in user_agent

    # Android captive portal tends to close early if internet is granted on welcome step.
    # Keep welcome as pure navigation for Android, but preserve current iOS behavior.
    if is_android:
        tariff_url = f"/tariffs?{urlencode({'mac': mac, 'router_id': router_id})}"
        return RedirectResponse(url=tariff_url, status_code=302)

    if not set_mikrotik_ah_access(mac, router_id, minutes=3, mode="PAY_WINDOW"):
        return JSONResponse({"error": "Ошибка активации доступа"}, status_code=500)

    conn = sqlite3.connect(os.path.join(BASE_DIR, 'gateway.db'))
    try:
        conn.execute(
            "INSERT INTO orders (mac_address, amount, status, router_id) VALUES (?, 0, 'PAY_WINDOW', ?)",
            (mac, router_id),
        )
        conn.commit()
    finally:
        conn.close()

    tariff_url = f"/tariffs?{urlencode({'mac': mac, 'router_id': router_id})}"
    return RedirectResponse(url=tariff_url, status_code=302)


@app.post("/get_free_trial")
async def get_free_trial(mac: str = Form(...), router_id: str = Form(...)):
    """Выдача 15 минут бесплатного доступа (1 раз в сутки)"""
    if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac or ""):
        return JSONResponse({"error": "Некорректный MAC-адрес"}, status_code=400)
    
    if check_trial_used_today(mac):
        return JSONResponse({"error": "Бесплатный доступ уже использован сегодня. Ждем вас завтра!"}, status_code=403)

    if not set_mikrotik_ah_access(mac, router_id, minutes=15, mode="TRIAL"):
        return JSONResponse({"error": "Ошибка активации доступа"}, status_code=500)
    
    conn = sqlite3.connect(os.path.join(BASE_DIR, 'gateway.db'))
    try:
        conn.execute(
            "INSERT INTO orders (mac_address, amount, status, router_id) VALUES (?, 0, 'TRIAL', ?)",
            (mac, router_id),
        )
        conn.commit()
    finally:
        conn.close()
    
    return {"message": "15 минут активировано! Нажмите 'Готово' и пользуйтесь."}


@app.post("/payment_result")
async def payment_result(request: Request):
    """Принимает callback от FreedomPay и активирует оплаченный доступ"""
    try:
        form_data = await request.form()
        params = dict(form_data)

        # Проверка подписи
        if params.get('pg_sig') != get_signature("payment_result", params, SECRET_KEY):
            logger.warning("Invalid signature in payment callback")
            return Response(content="Invalid signature", status_code=400)
        
        # Проверка успешной оплаты
        if params.get('pg_result') != '1':
            logger.info(f"Payment failed: {params.get('pg_result')}")
            return Response(content="Payment not successful", status_code=400)

        payment_order_id = (params.get('pg_order_id') or '').strip()
        mac = params.get('pg_param1')
        router_id = params.get('pg_param2')
        amount = int(float(params.get('pg_amount', 0)))

        if not mac:
            description = params.get('pg_description', '')
            mac_match = re.search(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})", description)
            if mac_match:
                mac = mac_match.group(1)

        if not mac or not router_id:
            conn = sqlite3.connect(os.path.join(BASE_DIR, 'gateway.db'))
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

        # Определение времени доступа
        if amount >= 2000:
            minutes = 1440  # 24 часа
        elif amount >= 900:
            minutes = 180   # 3 часа
        elif amount >= 400:
            minutes = 60    # 1 час
        else:
            minutes = 5     # тест 100₸

        # Активация доступа
        if not mac or not set_mikrotik_ah_access(mac, router_id, minutes, mode="PAID"):
            logger.error(f"Failed to activate paid access for {mac}")
            return Response(content="Activation failed", status_code=500)

        # Запись в БД
        conn = sqlite3.connect(os.path.join(BASE_DIR, 'gateway.db'))
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
            logger.info(f"Payment processed: {amount}₸ for {mac[:8]}*** ({minutes}min)")
        finally:
            conn.close()

        return Response(content="OK", status_code=200)
    
    except Exception as e:
        logger.error(f"Payment processing error: {str(e)[:200]}")
        return Response(content="Internal error", status_code=500)

@app.get("/success", response_class=HTMLResponse)
async def success(
    request: Request,
    mac: str = "00:00:00:00:00:00",
    router_id: str = "astana_01",
    minutes: int = 60,
    amount: int = 0,
):
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