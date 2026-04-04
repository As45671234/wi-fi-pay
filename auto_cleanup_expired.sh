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

LOGDIR="$HOME/.local/log"
mkdir -p "$LOGDIR"
LOGFILE="$LOGDIR/wifi-pay-cleanup.log"
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
from datetime import datetime
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

DRY_RUN = sys.argv[1].lower() == 'true' if len(sys.argv) > 1 else True

def parse_ends_at(ends_at_str):
    """Парсит строку времени 'Ends at' и возвращает Unix timestamp"""
    if not ends_at_str or ends_at_str == 'N/A':
        return None
    try:
        dt = datetime.strptime(ends_at_str, "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp())
    except:
        return None

def get_current_time_info():
    """Получает информацию о текущем времени на VPS"""
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S"), int(now.timestamp())

def process_router(router):
    """Обработать один роутер в отдельном потоке"""
    router_id = router.get('id', 'unknown')
    router_ip = router.get('ip', 'unknown')
    
    results = {
        'router_id': router_id,
        'router_ip': router_ip,
        'checked': 0,
        'deleted': 0,
        'messages': [],
        'status': 'ERROR'
    }
    
    try:
        connection = routeros_api.RouterOsApiPool(
            router_ip,
            username=router.get('user'),
            password=router.get('pass'),
            port=router.get('port', 8728),
            plaintext_login=True,
        )
        api = connection.get_api()
        
        current_time_str, current_timestamp = get_current_time_info()
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
            
            if any(x in comment for x in ['PAY_WINDOW_', 'TRIAL_', 'PAID_']):
                checked_count += 1
                ends_at_ts = parse_ends_at(ends_at_str)
                
                if ends_at_ts is None:
                    results['messages'].append(f"  ℹ️  {mac} | пропущена (N/A)")
                    continue
                
                if ends_at_ts < current_timestamp:
                    expired_count += 1
                    expired_macs.add(mac)
                    
                    if not DRY_RUN:
                        try:
                            binding.call('remove', arguments={'.id': binding_id})
                            results['deleted'] += 1
                        except Exception as e:
                            results['messages'].append(f"  ⚠️  Ошибка {mac}: {str(e)[:40]}")
                    else:
                        results['deleted'] += 1
                else:
                    remaining = (ends_at_ts - current_timestamp) // 60
                    results['messages'].append(f"  ✓ {mac} | осталось {remaining}м")
        
        # Очистка ACTIVE/COOKIE/HOST
        if expired_macs and not DRY_RUN:
            try:
                active = api.get_resource('/ip/hotspot/active')
                for a in active.call('print'):
                    if (a.get('mac-address') or '').upper() in expired_macs:
                        try:
                            active.call('remove', arguments={'.id': a.get('id') or a.get('.id')})
                        except:
                            pass
            except:
                pass
            
            try:
                cookie = api.get_resource('/ip/hotspot/cookie')
                for c in cookie.call('print'):
                    if (c.get('mac-address') or '').upper() in expired_macs:
                        try:
                            cookie.call('remove', arguments={'.id': c.get('id') or c.get('.id')})
                        except:
                            pass
            except:
                pass
            
            try:
                host = api.get_resource('/ip/hotspot/host')
                for h in host.call('print'):
                    if (h.get('mac-address') or '').upper() in expired_macs:
                        try:
                            host.call('remove', arguments={'.id': h.get('id') or h.get('.id')})
                        except:
                            pass
            except:
                pass
        
        results['checked'] = checked_count
        results['status'] = 'OK'
        
    except Exception as e:
        results['messages'].append(f"  ❌ {str(e)[:60]}")
        results['status'] = 'ERROR'
    
    return results

# MAIN
current_time_str, current_timestamp = get_current_time_info()
print(f"🕐 Текущее время VPS: {current_time_str}\n")

if DRY_RUN:
    print("⚠️  РЕЖИМ: DRY-RUN (без удалений!)\n")
else:
    print("🔴 РЕЖИМ: РЕАЛЬНОГО УДАЛЕНИЯ\n")

try:
    with open('routers_config.json') as f:
        routers = json.load(f)
except Exception as e:
    print(f"❌ Ошибка чтения routers_config.json: {e}")
    sys.exit(1)

total_checked = 0
total_deleted = 0

# Обработка роутеров в ПАРАЛЛЕЛЬНЫХ потоках (макс 4 одновременно)
with ThreadPoolExecutor(max_workers=4) as executor:
    futures = {executor.submit(process_router, r): r for r in routers}
    
    for future in as_completed(futures, timeout=40):  # 40 сек на всё
        try:
            results = future.result(timeout=8)  # 8 сек на один роутер
            
            if results['status'] == 'OK':
                print(f"✓ {results['router_id']} ({results['router_ip']}): {results['checked']} проверено, {results['deleted']} удалено")
            else:
                print(f"✗ {results['router_id']} ({results['router_ip']}): {results['status']}")
            
            for msg in results['messages'][:3]:  # Только первые 3 сообщения
                print(msg)
            
            total_checked += results['checked']
            total_deleted += results['deleted']
            
        except Exception as e:
            print(f"  ⏱️  Таймаут: {str(e)[:50]}")

print(f"\n════════════════════════════════════════════════════════════")
if DRY_RUN:
    print(f"📊 [DRY-RUN] Проверено {total_checked}, БУДЕТ удалено {total_deleted}")
else:
    print(f"📊 Проверено {total_checked}, удалено {total_deleted}")
print(f"════════════════════════════════════════════════════════════\n")

CLEANUP_SCRIPT

} | tee -a "$LOGFILE"

if [ "$DRY_RUN" = "true" ]; then
    echo "ℹ️  Это был сухой запуск. Используйте --apply для реального удаления:"
    echo "   ./auto_cleanup_expired.sh --apply"
fi
echo "✅ Проверка завершена. Логи: tail -f $LOGFILE"
