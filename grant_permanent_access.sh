#!/bin/bash
# Выдает бесконечный доступ конкретному MAC на конкретном роутере.
# Использование:
#   ./grant_permanent_access.sh <MAC> <ROUTER_ID>
# Пример:
#   ./grant_permanent_access.sh C2:01:4B:59:A5:12 astana_03

set -e

MAC="${1:-}"
ROUTER_ID="${2:-}"

if [ -z "$MAC" ] || [ -z "$ROUTER_ID" ]; then
    echo "Использование: ./grant_permanent_access.sh <MAC> <ROUTER_ID>"
    echo "Пример: ./grant_permanent_access.sh C2:01:4B:59:A5:12 astana_03"
    exit 1
fi

python3 - "$MAC" "$ROUTER_ID" << 'PYTHON_SCRIPT'
import json
import re
import sys

import routeros_api

mac = (sys.argv[1] if len(sys.argv) > 1 else '').strip().upper()
router_id = (sys.argv[2] if len(sys.argv) > 2 else '').strip()

if not re.fullmatch(r'([0-9A-F]{2}:){5}[0-9A-F]{2}', mac):
    print(f"❌ Некорректный MAC: {mac}")
    sys.exit(1)

with open('routers_config.json', 'r', encoding='utf-8') as f:
    routers = json.load(f)

router = next((r for r in routers if r.get('id') == router_id), None)
if not router:
    print(f"❌ Роутер не найден: {router_id}")
    sys.exit(1)

connection = None
try:
    connection = routeros_api.RouterOsApiPool(
        router['ip'],
        username=router['user'],
        password=router['pass'],
        port=router.get('port', 8728),
        plaintext_login=True,
    )
    api = connection.get_api()

    binding = api.get_resource('/ip/hotspot/ip-binding')
    active = api.get_resource('/ip/hotspot/active')
    user_res = api.get_resource('/ip/hotspot/user')
    cookie = api.get_resource('/ip/hotspot/cookie')
    host = api.get_resource('/ip/hotspot/host')
    sched = api.get_resource('/system/scheduler')

    user_name = f"T-{mac.replace(':', '')}"
    task_name = f"del_{mac.replace(':', '')}"

    print(f"🔧 Роутер: {router_id} ({router['ip']})")
    print(f"🔓 Выдаю бесконечный доступ для {mac}")

    # Удаляем старые временные записи для этого MAC, чтобы не мешали.
    removed = 0
    for row in binding.call('print', queries={'mac-address': mac}):
        try:
            binding.call('remove', arguments={'.id': row.get('id') or row.get('.id')})
            removed += 1
        except Exception:
            pass

    for row in active.call('print', queries={'mac-address': mac}):
        try:
            active.call('remove', arguments={'.id': row.get('id') or row.get('.id')})
            removed += 1
        except Exception:
            pass

    for row in user_res.call('print', queries={'name': user_name}):
        try:
            user_res.call('remove', arguments={'.id': row.get('id') or row.get('.id')})
            removed += 1
        except Exception:
            pass

    for row in cookie.call('print', queries={'mac-address': mac}):
        try:
            cookie.call('remove', arguments={'.id': row.get('id') or row.get('.id')})
            removed += 1
        except Exception:
            pass

    for row in host.call('print', queries={'mac-address': mac}):
        try:
            host.call('remove', arguments={'.id': row.get('id') or row.get('.id')})
            removed += 1
        except Exception:
            pass

    for row in sched.call('print', queries={'name': task_name}):
        try:
            sched.call('remove', arguments={'.id': row.get('id') or row.get('.id')})
            removed += 1
        except Exception:
            pass

    print(f"🧹 Удалено старых записей: {removed}")

    # Создаем постоянный bypass без scheduler.
    binding.call('add', arguments={
        'mac-address': mac,
        'type': 'bypassed',
        'comment': f'PAID_{mac}',
    })

    print(f"✅ Бесконечный доступ выдан для {mac} на {router_id}")
    print("ℹ️ Формат доступа: постоянный bypassed ip-binding без срока")

except Exception as e:
    print(f"❌ Ошибка: {str(e)[:200]}")
    sys.exit(1)
finally:
    if connection:
        try:
            connection.disconnect()
        except Exception:
            pass
PYTHON_SCRIPT
