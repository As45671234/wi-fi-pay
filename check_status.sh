#!/bin/bash
# Скрипт для проверки статуса устройства

MAC="${1:-22:1D:C8:99:FE:B0}"
DB_PATH="gateway.db"

echo "═══════════════════════════════════════════════"
echo "🔍 ПРОВЕРКА СТАТУСА ДЛЯ MAC: $MAC"
echo "═══════════════════════════════════════════════"
echo ""

# Проверка БД
echo "📊 ИСТОРИЯ В БАЗЕ ДАННЫХ:"
echo "─────────────────────────────────────────────"
sqlite3 $DB_PATH << EOF
.mode column
.headers on
SELECT 
    id,
    status,
    amount || ' ₸' as amount,
    datetime(created_at, 'localtime') as created_at,
    router_id,
    payment_order_id
FROM orders 
WHERE mac_address = '$MAC'
ORDER BY id DESC
LIMIT 5;
EOF
echo ""

# Проверка активной сессии через API
echo "🔐 АКТИВНЫЕ СЕССИИ В MIKROTIK:"
echo "─────────────────────────────────────────────"
python3 - "$MAC" << 'PYTHON_SCRIPT'
import routeros_api
import sys
import os
from datetime import datetime, timezone

MAC = sys.argv[1] if len(sys.argv) > 1 else "22:1D:C8:99:FE:B0"
ROUTER_IP = os.getenv('ROUTER_IP', '10.0.0.2')
ROUTER_USER = os.getenv('ROUTER_USER', 'admin')
ROUTER_PASS = os.getenv('ROUTER_PASS', 'kaspiwifiadmin2026')


def parse_scheduler_dt(date_str, time_str):
    try:
        return datetime.strptime(f"{date_str} {time_str}", "%b/%d/%Y %H:%M:%S")
    except Exception:
        return None


def format_remaining(target_dt):
    if not target_dt:
        return "N/A"
    remaining = int((target_dt - datetime.now()).total_seconds())
    if remaining <= 0:
        return "expired"
    hours, rem = divmod(remaining, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

try:
    connection = routeros_api.RouterOsApiPool(
        ROUTER_IP,
        username=ROUTER_USER,
        password=ROUTER_PASS,
        plaintext_login=True
    )
    api = connection.get_api()
    
    # Проверка active sessions
    active = api.get_resource('/ip/hotspot/active')
    sessions = active.call('print', queries={'mac-address': MAC})
    
    if sessions:
        for s in sessions:
            print(f"✓ Сессия найдена!")
            print(f"  User: {s.get('user', 'N/A')}")
            print(f"  IP: {s.get('address', 'N/A')}")
            print(f"  Uptime: {s.get('uptime', 'N/A')}")
            print(f"  Bytes In: {s.get('bytes-in', 'N/A')}")
            print(f"  Bytes Out: {s.get('bytes-out', 'N/A')}")
    else:
        print("✗ Активных сессий не найдено")
    
    # Проверка hotspot users
    print("\n👤 HOTSPOT USER:")
    print("─────────────────────────────────────────────")
    users = api.get_resource('/ip/hotspot/user')
    user_list = users.call('print', queries={'comment': MAC})
    
    if user_list:
        for u in user_list:
            print(f"✓ User найден!")
            print(f"  Name: {u.get('name', 'N/A')}")
            print(f"  Limit-uptime: {u.get('limit-uptime', 'N/A')}")
            print(f"  Comment: {u.get('comment', 'N/A')}")
    else:
        print("✗ User не найден")
    
    # Проверка IP bindings
    print("\n🔗 IP BINDINGS:")
    print("─────────────────────────────────────────────")
    bindings = api.get_resource('/ip/hotspot/ip-binding')
    binding_list = bindings.call('print', queries={'mac-address': MAC})
    
    if binding_list:
        for b in binding_list:
            print(f"✓ Binding найден!")
            print(f"  Type: {b.get('type', 'N/A')}")
            print(f"  Comment: {b.get('comment', 'N/A')}")
    else:
        print("✗ Bindings не найдены")
    
    # Проверка scheduler
    print("\n⏰ АВТО-ОЧИСТКА (SCHEDULER):")
    print("─────────────────────────────────────────────")
    sched = api.get_resource('/system/scheduler')
    mac_hex = MAC.replace(':', '')
    tasks = sched.call('print', queries={'name': f'del_{mac_hex}'})
    
    if tasks:
        for t in tasks:
            target_dt = parse_scheduler_dt(t.get('start-date', ''), t.get('start-time', ''))
            print(f"✓ Задача найдена!")
            print(f"  Name: {t.get('name', 'N/A')}")
            print(f"  Start: {t.get('start-date', 'N/A')} {t.get('start-time', 'N/A')}")
            print(f"  Remaining: {format_remaining(target_dt)}")
            print(f"  Comment: {t.get('comment', 'N/A')}")
    else:
        print("✗ Задач не найдено")
    
    connection.disconnect()
    
except Exception as e:
    print(f"❌ Ошибка: {e}")

PYTHON_SCRIPT

echo ""
echo "═══════════════════════════════════════════════"
echo "✅ Проверка завершена"
echo "═══════════════════════════════════════════════"
