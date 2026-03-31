#!/bin/bash
# Скрипт для очистки всех MAC-адресов и сессий на роутерах
# Использование: ./cleanup_routers.sh

set -e

echo "╔════════════════════════════════════════════════════════════╗"
echo "║      Очистка WiFi-Pay (все биндинги/юзеры/schedulers)    ║"
echo "╚════════════════════════════════════════════════════════════╝"

python3 << 'CLEANUP_SCRIPT'
import json
import sys
import routeros_api
import time
import re

try:
    with open('routers_config.json') as f:
        routers = json.load(f)
except Exception as e:
    print(f"❌ Ошибка чтения routers_config.json: {e}")
    sys.exit(1)

for router in routers:
    router_id = router.get('id', 'unknown')
    router_ip = router.get('ip', 'unknown')
    
    print(f"\n🔧 Очистка {router_id} ({router_ip})...")
    
    try:
        connection = routeros_api.RouterOsApiPool(
            router_ip,
            username=router.get('user'),
            password=router.get('pass'),
            port=router.get('port', 8728),
            plaintext_login=True,
        )
        api = connection.get_api()

        # Собираем MAC-адреса WiFi-Pay сессий, чтобы корректно удалить active/host/cookie.
        target_macs = set()
        mac_rx = re.compile(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")

        binding = api.get_resource('/ip/hotspot/ip-binding')
        for b in binding.call('print'):
            comment = (b.get('comment') or '')
            mac = (b.get('mac-address') or '').upper()
            if any(x in comment for x in ['PAY_WINDOW_', 'TRIAL_', 'PAID_']) and mac:
                target_macs.add(mac)

        user_res = api.get_resource('/ip/hotspot/user')
        for u in user_res.call('print'):
            comment = (u.get('comment') or '')
            if any(x in comment for x in ['PAY_WINDOW_', 'TRIAL_', 'PAID_']):
                m = mac_rx.search(comment)
                if m:
                    target_macs.add(m.group(0).upper())

        print(f"  • Найдено целевых MAC: {len(target_macs)}")

        # 1. Удалить ACTIVE сессии (до удаления binding)
        print("  1. Удаляю ACTIVE сессии...")
        active = api.get_resource('/ip/hotspot/active')
        active_sessions = active.call('print')
        count = 0
        for a in active_sessions:
            mac = (a.get('mac-address') or '').upper()
            user = (a.get('user') or '')
            # PAY_WINDOW активки тоже удаляем по MAC, PAID/TRIAL — по user T-*
            if (mac and mac in target_macs) or user.startswith('T-'):
                try:
                    active.call('remove', arguments={'.id': a.get('id') or a.get('.id')})
                    count += 1
                except Exception as e:
                    print(f"     ⚠️ Ошибка удаления active: {str(e)[:80]}")
        print(f"     ✓ Удалено {count} активных сессий")

        # 2. Удалить COOKIE по целевым MAC
        print("  2. Удаляю COOKIE...")
        cookie = api.get_resource('/ip/hotspot/cookie')
        cookies = cookie.call('print')
        count = 0
        for c in cookies:
            mac = (c.get('mac-address') or '').upper()
            if mac and (not target_macs or mac in target_macs):
                try:
                    cookie.call('remove', arguments={'.id': c.get('id') or c.get('.id')})
                    count += 1
                except Exception as e:
                    print(f"     ⚠️ Ошибка удаления cookie: {str(e)[:80]}")
        print(f"     ✓ Удалено {count} cookies")

        # 3. Удалить HOST по целевым MAC
        print("  3. Удаляю HOST entries...")
        host = api.get_resource('/ip/hotspot/host')
        hosts = host.call('print')
        count = 0
        for h in hosts:
            mac = (h.get('mac-address') or '').upper()
            if mac and (not target_macs or mac in target_macs):
                try:
                    host.call('remove', arguments={'.id': h.get('id') or h.get('.id')})
                    count += 1
                except Exception as e:
                    print(f"     ⚠️ Ошибка удаления host: {str(e)[:80]}")
        print(f"     ✓ Удалено {count} host entries")

        # 4. Удалить все IP-BINDINGS (PAY_WINDOW, TRIAL, PAID)
        print("  4. Удаляю IP-BINDINGS...")
        bindings = binding.call('print')
        count = 0
        for b in bindings:
            comment = b.get('comment', '')
            # Удалять только WiFi-Pay сессии (начинаются с PAY_WINDOW, TRIAL, PAID)
            if any(x in comment for x in ['PAY_WINDOW_', 'TRIAL_', 'PAID_', 'AUTOCLEAR_']):
                try:
                    binding.call('remove', arguments={'.id': b.get('id') or b.get('.id')})
                    count += 1
                except Exception as e:
                    print(f"     ⚠️ Ошибка удаления {comment}: {str(e)[:80]}")
        print(f"     ✓ Удалено {count} биндингов")

        # 5. Удалить HOTSPOT USERS с нашими комментариями
        print("  5. Удаляю HOTSPOT USERS...")
        users = user_res.call('print')
        count = 0
        for u in users:
            comment = u.get('comment', '')
            if any(x in comment for x in ['PAY_WINDOW_', 'TRIAL_', 'PAID_', 'AUTOCLEAR_']):
                try:
                    user_res.call('remove', arguments={'.id': u.get('id') or u.get('.id')})
                    count += 1
                except Exception as e:
                    print(f"     ⚠️ Ошибка удаления юзера: {str(e)[:80]}")
        print(f"     ✓ Удалено {count} юзеров")

        # 6. Удалить SCHEDULERS для автоочистки
        print("  6. Удаляю SCHEDULERS...")
        sched = api.get_resource('/system/scheduler')
        schedulers = sched.call('print')
        count = 0
        for s in schedulers:
            name = s.get('name', '')
            comment = s.get('comment', '')
            if name.startswith('del_') or 'AUTOCLEAR_' in comment:
                try:
                    sched.call('remove', arguments={'.id': s.get('id') or s.get('.id')})
                    count += 1
                except Exception as e:
                    print(f"     ⚠️ Ошибка удаления scheduler: {str(e)[:80]}")
        print(f"     ✓ Удалено {count} schedulers")

        # 7. Проверить статус hotspot
        print("  7. Проверяю статус HOTSPOT...")
        hotspot = api.get_resource('/ip/hotspot/profile')
        profiles = hotspot.call('print')
        if profiles:
            print(f"     ✓ Hotspot профили: {len(profiles)} штук")
            for p in profiles:
                print(f"       - {p.get('name')}")
        else:
            print(f"     ❌ ВНИМАНИЕ: Hotspot профили НЕ найдены!")
            print(f"        Hotspot может быть отключен на {router_id}!")
        
        connection.disconnect()
        print(f"  ✅ {router_id} очищен успешно")
        
    except Exception as e:
        print(f"  ❌ ОШИБКА подключения к {router_id}: {str(e)[:150]}")

print("\n╔════════════════════════════════════════════════════════════╗")
print("║  ✅ Очистка завершена                                     ║")
print("╚════════════════════════════════════════════════════════════╝")
print("\n🚀 Теперь перезагрузи wifi-pay:")
print("   sudo systemctl restart wifi-pay")
print("\nИ тестируй:")
print("   curl http://localhost:8000/health")
CLEANUP_SCRIPT

