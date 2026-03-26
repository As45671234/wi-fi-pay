#!/bin/bash
# Автоматическая диагностика и восстановление WiFi-Pay
# Используй: ./fix_wifi_pay.sh

echo "╔════════════════════════════════════════════════════════════╗"
echo "║         WiFi-Pay Automatic Recovery Guide                 ║"
echo "╚════════════════════════════════════════════════════════════╝"

# Цвета
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo ""
echo -e "${BLUE}ШАГИ ДЛЯ ВОССТАНОВЛЕНИЯ:${NC}"
echo ""

echo "1️⃣  МЯГКАЯ ОЧИСТКА (рекомендуется первым)"
echo "────────────────────────────────────────"
echo ""
echo "Это удалит все старые WiFi-Pay сессии с роутеров,"
echo "но сам роутер не будет перезагружен."
echo ""
echo "Запусти:"
echo -e "  ${YELLOW}./cleanup_routers.sh${NC}"
echo ""
echo "Потом:"
echo -e "  ${YELLOW}sudo systemctl restart wifi-pay${NC}"
echo ""
echo "И проверь:"
echo -e "  ${YELLOW}curl http://localhost:8000/health${NC}"
echo ""

echo "---"
echo ""

echo "2️⃣  ЕСЛИ МЯГКАЯ ОЧИСТКА НЕ ПОМОГЛА → ПЕРЕЗАГРУЗИТЬ РОУТЕРЫ"
echo "────────────────────────────────────────"
echo ""
echo "Это перезагрузит роутеры (интернет отключится на 30-60сек)."
echo ""
echo "Все роутеры:"
echo -e "  ${YELLOW}./reboot_routers.sh${NC}"
echo ""
echo "Или один конкретный (например, astana_03 из которого проблемы):"
echo -e "  ${YELLOW}./reboot_routers.sh astana_03${NC}"
echo ""
echo "Подожди ~1 минуту, потом проверь:"
echo -e "  ${YELLOW}./diagnose.sh${NC}"
echo ""

echo "---"
echo ""

echo "3️⃣  ЕСЛИ ВСЕ РАВНО НЕ ПОМОГАЕТ"
echo "────────────────────────────────────────"
echo ""
echo "Проверь логи:"
echo -e "  ${YELLOW}sudo journalctl -u wifi-pay -f${NC}"
echo ""
echo "Диагностируй конкретный MAC (82:10:24:99:CE:76 - пример):"
echo -e "  ${YELLOW}curl 'http://localhost:8000/debug?mac=82:10:24:99:CE:76&router_id=astana_01'${NC}"
echo ""
echo "Проверь что на роутере вообще включен hotspot:"
echo -e "  ${YELLOW}ssh ubuntu@10.0.0.2${NC}  (или IP другого роутера)"
echo -e "  ${YELLOW}/ip hotspot settings print${NC}"
echo ""

echo "---"
echo ""

echo "ЧЕКЛИСТ ДО НАЧАЛА:"
echo "✓ WireGuard туннель работает?        → sudo wg show"
echo "✓ Можешь пингануть роутеры?         → ping -c 1 10.0.0.2"
echo "✓ WiFi-Pay сервис работает?         → sudo systemctl status wifi-pay"
echo "✓ Hotspot профили есть на роутере?  → /ip hotspot profile print"
echo ""
echo "════════════════════════════════════════════════════════════"
echo ""
echo "Рекомендуемая последовательность:"
echo ""
echo "1. Запусти cleanup_routers.sh"
echo "2. Перезагрузи wifi-pay сервис"
echo "3. Тестируй портал"
echo "4. Если не работает → запусти ./diagnose.sh"
echo "5. Если diagnose покажет проблемы → перезагрузи роутеры"
echo ""
