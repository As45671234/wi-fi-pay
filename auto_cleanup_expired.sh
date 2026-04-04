#!/bin/bash
# Периодическая очистка истекших сессий (запускать раз в час через cron)
# Использование:
#   ./auto_cleanup_expired.sh            # сухой запуск (без удалений!)
#   ./auto_cleanup_expired.sh --apply    # реальное удаление
# 
# В cron добавить:
#   0 * * * * /opt/wifi-pay/auto_cleanup_expired.sh --apply 2>&1 | logger
# 
# ОСТОРОЖНО: удаляет ТОЛЬКО истекшие (Ends at < текущее время) записи!
# Проверяет реальное время перед удалением!

set -e

DRY_RUN=true
if [ "$1" = "--apply" ]; then
    DRY_RUN=false
fi

LOGFILE="/var/log/wifi-pay-cleanup.log"
NOW=$(date '+%Y-%m-%d %H:%M:%S')
TIMESTAMP=$(date '+%s')

{
    echo "════════════════════════════════════════════════════════════"
    echo "[$NOW] Начало периодической очистки истекших сессий"
    echo "════════════════════════════════════════════════════════════"
    
    python3 - "$DRY_RUN" << 'CLEANUP_SCRIPT'
import json
import sys
import routeros_api
import re
from datetime import datetime
import time

# Получаем флаг dry-run из аргумента
DRY_RUN = sys.argv[1].lower() == 'true' if len(sys.argv) > 1 else True

def parse_ends_at(ends_at_str):
    """Парсит строку времени 'Ends at' и возвращает Unix timestamp"""
    if not ends_at_str or ends_at_str == 'N/A':
        return None
    try:
        # Формат: "2026-04-04 15:20:19"
        dt = datetime.strptime(ends_at_str, "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp())
    except Exception as e:
        print(f"  ⚠️  Ошибка парсинга времени '{ends_at_str}': {e}")
        return None

def get_current_time_info():
    """Получает информацию о текущем времени на VPS"""
    now = datetime.now()
    timestamp = int(now.timestamp())
    return now.strftime("%Y-%m-%d %H:%M:%S"), timestamp

current_time_str, current_timestamp = get_current_time_info()
print(f"🕐 Текущее время VPS: {current_time_str} (timestamp: {current_timestamp})")

# Режим работы
if DRY_RUN:
    print("⚠️  РЕЖИМ: DRY-RUN (только предпросмотр, без удалений!)")
    print("    Используйте: --apply для реального удаления\n")
else:
    print("🔴 РЕЖИМ: РЕАЛЬНОГО УДАЛЕНИЯ ВКЛЮЧЕН!\n")

try:
    with open('routers_config.json') as f:
        routers = json.load(f)
except Exception as e:
    print(f"❌ Ошибка чтения routers_config.json: {e}")
    sys.exit(1)

total_deleted = 0
total_checked = 0

for router in routers:
    router_id = router.get('id', 'unknown')
    router_ip = router.get('ip', 'unknown')
    
    print(f"\n🔍 Проверка {router_id} ({router_ip})...")
    
    try:
        connection = routeros_api.RouterOsApiPool(
            router_ip,
            username=router.get('user'),
            password=router.get('pass'),
            port=router.get('port', 8728),
            plaintext_login=True,
        )
        api = connection.get_api()
        
        # Получаем время на роутере для контроля
        system = api.get_resource('/system/identity')
        identity = system.call('print')
        
        # Получаем список IP-BINDINGS с нашими комментариями
        binding = api.get_resource('/ip/hotspot/ip-binding')
        bindings = binding.call('print')
        
        expired_macs = set()
        expired_count = 0
        checked_count = 0
        
        for b in bindings:
            comment = b.get('comment', '')
            mac = (b.get('mac-address') or '').upper()
            binding_id = b.get('id') or b.get('.id')
            ends_at_str = b.get('ends-at', 'N/A')
            
            # Проверяем только наши биндинги
            if any(x in comment for x in ['PAY_WINDOW_', 'TRIAL_', 'PAID_']):
                checked_count += 1
                total_checked += 1
                
                ends_at_ts = parse_ends_at(ends_at_str)
                
                if ends_at_ts is None:
                    # Если нет времени окончания или N/A, пропускаем (это может быть bypassed)
                    print(f"  ℹ️  {mac} | {comment} | Ends at: {ends_at_str} (пропущена)")
                    continue
                
                # КРИТИЧНА ПРОВЕРКА: только если ДЕЙСТВИТЕЛЬНО истекло
                if ends_at_ts < current_timestamp:
                    expired_count += 1
                    total_deleted += 1
                    expired_macs.add(mac)
                    
                    # Удаляем IP-BINDING (или показываем в dry-run)
                    if not DRY_RUN:
                        try:
                            binding.call('remove', arguments={'.id': binding_id})
                            print(f"  ✖️  УДАЛЕНА: {mac} | {comment} | было до {ends_at_str}")
                        except Exception as e:
                            print(f"  ⚠️  Ошибка удаления {mac}: {str(e)[:60]}")
                    else:
                        print(f"  [DRY] БУДЕТ УДАЛЕНА: {mac} | {comment} | истекла {ends_at_str}")
                else:
                    remaining = ends_at_ts - current_timestamp
                    mins = remaining // 60
                    print(f"  ✓ АКТИВНА: {mac} | осталось {mins}м | {ends_at_str}")
        
        # Если были истекшие, удаляем связанные ACTIVE сессии
        if expired_macs:
            print(f"\n  🧹 Очистка {len(expired_macs)} истекших: удаляю ACTIVE сессии...")
            active = api.get_resource('/ip/hotspot/active')
            active_sessions = active.call('print')
            active_count = 0
            for a in active_sessions:
                mac = (a.get('mac-address') or '').upper()
                if mac in expired_macs:
                    if not DRY_RUN:
                        try:
                            active.call('remove', arguments={'.id': a.get('id') or a.get('.id')})
                            active_count += 1
                        except Exception as e:
                            pass
                    else:
                        active_count += 1
            if active_count > 0:
                print(f"     ✓ {'Удалено' if not DRY_RUN else '[DRY] Будет удалено'} {active_count} active сессий")
            
            # Удаляем COOKIE
            print(f"  🧹 Удаляю COOKIE ({len(expired_macs)} MAC)...")
            cookie = api.get_resource('/ip/hotspot/cookie')
            cookies = cookie.call('print')
            cookie_count = 0
            for c in cookies:
                mac = (c.get('mac-address') or '').upper()
                if mac in expired_macs:
                    if not DRY_RUN:
                        try:
                            cookie.call('remove', arguments={'.id': c.get('id') or c.get('.id')})
                            cookie_count += 1
                        except Exception as e:
                            pass
                    else:
                        cookie_count += 1
            if cookie_count > 0:
                print(f"     ✓ {'Удалено' if not DRY_RUN else '[DRY] Будет удалено'} {cookie_count} cookies")
            
            # Удаляем HOST
            print(f"  🧹 Удаляю HOST entries ({len(expired_macs)} MAC)...")
            host = api.get_resource('/ip/hotspot/host')
            hosts = host.call('print')
            host_count = 0
            for h in hosts:
                mac = (h.get('mac-address') or '').upper()
                if mac in expired_macs:
                    if not DRY_RUN:
                        try:
                            host.call('remove', arguments={'.id': h.get('id') or h.get('.id')})
                            host_count += 1
                        except Exception as e:
                            pass
                    else:
                        host_count += 1
            if host_count > 0:
                print(f"     ✓ {'Удалено' if not DRY_RUN else '[DRY] Будет удалено'} {host_count} host entries")
        
        print(f"  ► Итог: проверено {checked_count}, удалено {expired_count}")
        
    except Exception as e:
        print(f"❌ Ошибка подключения к {router_id}: {str(e)[:80]}")
        continue

print(f"\n════════════════════════════════════════════════════════════")
if DRY_RUN:
    print(f"📊 [DRY-RUN] Проверено {total_checked} записей, БУДЕТ удалено {total_deleted} истекших")
else:
    print(f"📊 Проверено {total_checked} записей, удалено {total_deleted} истекших")
print(f"════════════════════════════════════════════════════════════\n")

CLEANUP_SCRIPT

} | tee -a "$LOGFILE"

if [ "$DRY_RUN" = "true" ]; then
    echo "ℹ️  Это был сухой запуск. Используйте --apply для реального удаления:"
    echo "   ./auto_cleanup_expired.sh --apply"
fi
echo "✅ Проверка завершена. Логи: tail -f $LOGFILE"
