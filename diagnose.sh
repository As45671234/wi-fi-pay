#!/bin/bash
# Скрипт диагностики WiFi-Pay
# Использование: ./diagnose.sh

set -e

echo "╔════════════════════════════════════════════════════════════╗"
echo "║            WiFi-Pay Diagnostic Tool                       ║"
echo "╚════════════════════════════════════════════════════════════╝"

# Цвета
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

check_status() {
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✓${NC} $1"
    else
        echo -e "${RED}✗${NC} $1"
        return 1
    fi
}

# 1. Системная информация
echo ""
echo -e "${BLUE}1. СИСТЕМНАЯ ИНФОРМАЦИЯ${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

OS=$(lsb_release -d 2>/dev/null | cut -f2)
echo "OS: $OS"

KERNEL=$(uname -r)
echo "Kernel: $KERNEL"

UPTIME=$(uptime -p)
echo "Uptime: $UPTIME"

LOAD=$(cat /proc/loadavg | awk '{print $1, $2, $3}')
echo "Load Average: $LOAD"

# 2. WiFi-Pay сервис
echo ""
echo -e "${BLUE}2. WIFI-PAY СЕРВИС${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if sudo systemctl is-active --quiet wifi-pay; then
    echo -e "${GREEN}✓${NC} Сервис работает"
    
    # Статус
    STATUS=$(sudo systemctl status wifi-pay 2>&1 | grep "Active:" | xargs)
    echo "  $STATUS"
    
    # Memory
    MEM=$(ps aux | grep "[u]vicorn main:app" | awk '{print $6}' | head -1)
    if [ ! -z "$MEM" ]; then
        echo "  Memory: ${MEM}KB"
    fi
else
    echo -e "${RED}✗${NC} Сервис НЕ работает"
    echo "  Запусти: sudo systemctl start wifi-pay"
fi

# 3. Обработка портов
echo ""
echo -e "${BLUE}3. ПОРТЫ${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if sudo lsof -i :8000 &>/dev/null; then
    echo -e "${GREEN}✓${NC} Порт 8000 слушается"
else
    echo -e "${RED}✗${NC} Порт 8000 НЕ слушается"
fi

# 4. WireGuard
echo ""
echo -e "${BLUE}4. WIREGUARD${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if sudo wg show wg0 &>/dev/null; then
    echo -e "${GREEN}✓${NC} WireGuard интерфейс wg0 есть"
    
    WG_IP=$(ip addr show wg0 2>/dev/null | grep "inet " | awk '{print $2}')
    if [ ! -z "$WG_IP" ]; then
        echo "  IP: $WG_IP"
    fi
    
    PEERS=$(sudo wg show wg0 peers 2>/dev/null | wc -l)
    echo "  Peers: $PEERS"
else
    echo -e "${YELLOW}⚠${NC} WireGuard интерфейс wg0 НЕ найден (может быть OK если используешь другое имя)"
fi

# 5. Роутеры доступность
echo ""
echo -e "${BLUE}5. ДОСТУПНОСТЬ РОУТЕРОВ${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

python3 << 'ROUTER_CHECK'
import json
import sys
import routeros_api
import time
from datetime import datetime

try:
    with open('routers_config.json', encoding='utf-8-sig') as f:
        routers = json.load(f)
except Exception as e:
    print(f"✗ Ошибка чтения routers_config.json: {e}")
    sys.exit(1)

connected = 0
failed = 0

for router in routers:
    router_id = router.get('id', 'unknown')
    router_ip = router.get('ip', 'unknown')
    
    status = "○"
    details = ""
    
    try:
        connection = routeros_api.RouterOsApiPool(
            router_ip,
            username=router.get('user'),
            password=router.get('pass'),
            port=router.get('port', 8728),
            plaintext_login=True,
        )
        api = connection.get_api()
        
        # System info
        identity = api.get_resource('/system/identity').call('print')
        name = identity[0].get('name', 'N/A') if identity else 'N/A'
        
        # Hotspot status
        hotspot = api.get_resource('/ip/hotspot/profile').call('print')
        hotspot_status = f"Hotspot: {'OK' if hotspot else 'OFF'}"
        
        # Active sessions
        active = api.get_resource('/ip/hotspot/active').call('print')
        sessions = f"Sessions: {len(active)}"
        
        print(f"  ✓ {router_id:15} {router_ip:12} {name:20} {hotspot_status:15} {sessions}")
        connected += 1
        
        connection.disconnect()
        time.sleep(0.3)
    except ConnectionRefusedError:
        print(f"  ✗ {router_id:15} {router_ip:12} Отказано в соединении")
        failed += 1
    except TimeoutError:
        print(f"  ✗ {router_id:15} {router_ip:12} Timeout (не отвечает)")
        failed += 1
    except Exception as e:
        print(f"  ✗ {router_id:15} {router_ip:12} {str(e)[:40]}")
        failed += 1

print(f"\n  Результат: {connected} OK / {failed} сбой")
if failed > 0:
    print(f"\n  ⚠️  {failed} роутеров недоступны:")
    print("     - Проверь WireGuard туннель: sudo wg show")
    print("     - Ping роутеры: ping -c 1 10.0.0.2")
    print("     - Firewall: sudo ufw status")
ROUTER_CHECK

# 6. HTTP тесты
echo ""
echo -e "${BLUE}6. HTTP ТЕСТЫ${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Тест /health
curl -s http://localhost:8000/health > /dev/null 2>&1
if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓${NC} /health endpoint отвечает"
else
    echo -e "${RED}✗${NC} /health endpoint НЕ отвечает"
    echo "  Сервис может еще стартовать, попробуй через 5 секунд"
fi

# 7. База данных
echo ""
echo -e "${BLUE}7. БАЗА ДАННЫХ${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [ -f "gateway.db" ]; then
    SIZE=$(du -h gateway.db | awk '{print $1}')
    ROWS=$(sqlite3 gateway.db "SELECT COUNT(*) FROM orders" 2>/dev/null || echo "0")
    echo -e "${GREEN}✓${NC} gateway.db найден"
    echo "  Размер: $SIZE"
    echo "  Записей: $ROWS"
else
    echo -e "${YELLOW}⚠${NC} gateway.db не найден (будет создан при первом запуске)"
fi

# 8. Логи
echo ""
echo -e "${BLUE}8. ПОСЛЕДНИЕ ОШИБКИ В ЛОГАХ${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

ERRORS=$(sudo journalctl -u wifi-pay -n 500 2>/dev/null | grep -i "error\|❌" | tail -5)
if [ ! -z "$ERRORS" ]; then
    echo "  Найденные ОШИБКИ:"
    echo "$ERRORS" | while read line; do
        echo "  $line"
    done
else
    echo -e "${GREEN}✓${NC} Ошибок не найдено"
fi

# 9. Дисковое пространство
echo ""
echo -e "${BLUE}9. ДИСКОВОЕ ПРОСТРАНСТВО${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

DISK=$(df -h / | tail -1 | awk '{print $5}')
echo "  Использовано /: $DISK"

if [ "${DISK%\%}" -gt 80 ]; then
    echo -e "${RED}  ⚠️  Мало места на диске!${NC}"
fi

# Итого
echo ""
echo "╔════════════════════════════════════════════════════════════╗"
echo "║  Диагностика завершена                                    ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""
echo "📖 Для дополнительной помощи:"
echo "   - Документация: cat DEPLOYMENT.md"
echo "   - Смотреть логи: sudo journalctl -u wifi-pay -f"
echo "   - Проверить /health: curl http://localhost:8000/health"
echo ""
