#!/bin/bash
# Скрипт для перезагрузки роутеров
# Использование: ./reboot_routers.sh [all|имя_роутера]

set -e

ROUTER_TO_REBOOT="${1:-all}"

echo "╔════════════════════════════════════════════════════════════╗"
echo "║             Перезагрузка MikroTik роутеров                ║"
echo "║        ВНИМАНИЕ: это отключит интернет на 30-60 сек       ║"
echo "╚════════════════════════════════════════════════════════════╝"

read -p "⚠️  Ты уверен? Это отключит интернет. (yes/no): " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
    echo "❌ Отменено"
    exit 0
fi

python3 << 'REBOOT_SCRIPT'
import json
import sys
import routeros_api
import time

ROUTER_TO_REBOOT = sys.argv[1] if len(sys.argv) > 1 else "all"

try:
    with open('routers_config.json') as f:
        routers = json.load(f)
except Exception as e:
    print(f"❌ Ошибка чтения routers_config.json: {e}")
    sys.exit(1)

for router in routers:
    router_id = router.get('id', 'unknown')
    
    # Если указан конкретный роутер - только его
    if ROUTER_TO_REBOOT != "all" and router_id != ROUTER_TO_REBOOT:
        continue
    
    router_ip = router.get('ip', 'unknown')
    
    print(f"\n🔄 Перезагружаю {router_id} ({router_ip})...", end=' ')
    sys.stdout.flush()
    
    try:
        connection = routeros_api.RouterOsApiPool(
            router_ip,
            username=router.get('user'),
            password=router.get('pass'),
            port=router.get('port', 8728),
            plaintext_login=True,
        )
        api = connection.get_api()
        
        # Отправляем команду перезагрузки
        api.get_resource('/system/reboot').call('reboot')
        print("✓ Команда отправлена")
        
        connection.disconnect()
        
        # Даем роутеру время на перезагрузку
        time.sleep(5)
        
    except Exception as e:
        print(f"⚠️ {str(e)[:80]}")

print("\n╔════════════════════════════════════════════════════════════╗")
print("║  🔄 Роутеры перезагружаются (30-60 сек)...               ║")
print("║  Интернет вернется через ~1 минуту                       ║")
print("╚════════════════════════════════════════════════════════════╝")
print("\n💡 Подожди 1-2 минуты и проверь:")
print("   ./diagnose.sh")
REBOOT_SCRIPT

