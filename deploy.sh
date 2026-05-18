#!/bin/bash
# Скрипт развертывания WiFi-Pay на VPS
# Использование: ./deploy.sh [production|development]

set -e

ENVIRONMENT="${1:-production}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_SERVICE="wifi-pay"
VENV_DIR="$APP_DIR/venv"

echo "╔════════════════════════════════════════════════════════════╗"
echo "║        WiFi-Pay Development Deploy Script                 ║"
echo "║        Environment: $ENVIRONMENT                          ║"
echo "╚════════════════════════════════════════════════════════════╝"

# 1. Проверка зависимостей
echo ""
echo "📦 Шаг 1: Проверка зависимостей..."
if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 не установлен!"
    exit 1
fi
echo "✓ Python3: $(python3 --version)"

if ! command -v git &> /dev/null; then
    echo "❌ Git не установлен!"
    exit 1
fi
echo "✓ Git установлен"

# 2. Создание venv если его нет
echo ""
echo "🐍 Шаг 2: Подготовка виртуального окружения..."
if [ ! -d "$VENV_DIR" ]; then
    echo "  Создаю venv в $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
    echo "  ✓ venv создан"
else
    echo "  ✓ venv уже существует"
fi

source "$VENV_DIR/bin/activate"

# 3. Установка зависимостей
echo ""
echo "📚 Шаг 3: Установка зависимостей Python..."
pip install --upgrade pip setuptools wheel > /dev/null 2>&1 || true

# Проверяем requirements.txt или устанавливаем стандартные
if [ -f "$APP_DIR/requirements.txt" ]; then
    echo "  Установка из requirements.txt..."
    pip install -q -r "$APP_DIR/requirements.txt"
    echo "  ✓ Зависимости установлены"
else
    echo "  requirements.txt не найден, устанавливаю стандартные..."
    pip install -q fastapi uvicorn routeros-api[WireGuard]
    echo "  ✓ Стандартные зависимости установлены"
fi

# 4. Проверка конфигурации
echo ""
echo "⚙️  Шаг 4: Проверка конфигурации..."
if [ ! -f "$APP_DIR/routers_config.json" ]; then
    echo "❌ routers_config.json не найден!"
    exit 1
fi

# Парсим JSON и проверяем количество роутеров
ROUTER_COUNT=$(python3 -c "import json; print(len(json.load(open('$APP_DIR/routers_config.json', encoding='utf-8-sig'))))" 2>/dev/null || echo "0")
if [ "$ROUTER_COUNT" -eq 0 ]; then
    echo "❌ routers_config.json пуст или некорректен!"
    exit 1
fi
echo "✓ Найдено $ROUTER_COUNT роутеров в конфигурации"

# 5. Проверка подключения к роутерам
echo ""
echo "🔌 Шаг 5: Проверка подключения к роутерам..."
python3 << 'PYTHON_CHECK'
import json
import sys
import routeros_api
import time

try:
    with open('routers_config.json', encoding='utf-8-sig') as f:
        routers = json.load(f)
except Exception as e:
    print(f"❌ Ошибка чтения routers_config.json: {e}")
    sys.exit(1)

connected = 0
failed = 0

for router in routers:
    router_id = router.get('id', 'unknown')
    router_ip = router.get('ip', 'unknown')
    
    try:
        print(f"  Проверяю {router_id} ({router_ip})...", end=' ')
        sys.stdout.flush()
        
        connection = routeros_api.RouterOsApiPool(
            router_ip,
            username=router.get('user'),
            password=router.get('pass'),
            port=router.get('port', 8728),
            plaintext_login=True,
        )
        api = connection.get_api()
        
        # Пытаемся прочитать identity
        identity = api.get_resource('/system/identity').call('print')
        if identity:
            name = identity[0].get('name', 'N/A')
            print(f"✓ OK (Identity: {name})")
            connected += 1
        else:
            print(f"⚠️  Связь есть, но Identity пуст")
            connected += 1
            
        connection.disconnect()
        time.sleep(0.5)
    except ConnectionRefusedError:
        print(f"❌ Отказано в соединении")
        failed += 1
    except TimeoutError:
        print(f"❌ Timeout (роутер не отвечает)")
        failed += 1
    except Exception as e:
        print(f"❌ {str(e)[:50]}")
        failed += 1

print(f"\n  Результат: {connected} OK, {failed} ОШИБОК")

if connected == 0:
    print("\n⚠️  ВНИМАНИЕ: Ни один роутер не доступен! Проверьте:")
    print("   - WireGuard туннель работает?")
    print("   - IPs в routers_config.json корректны?")
    print("   - Роутеры включены?")
    sys.exit(1)

sys.exit(0)
PYTHON_CHECK

if [ $? -ne 0 ]; then
    echo "❌ Проверка подключения не пройдена!"
    exit 1
fi

# 6. Синтаксис проверка
echo ""
echo "✓ Шаг 6: Проверка синтаксиса Python..."
python3 -m py_compile "$APP_DIR/main.py" || {
    echo "❌ Синтаксическая ошибка в main.py!"
    exit 1
}
echo "✓ Синтаксис OK"

# 7. Перезагруз сервиса (если это production)
if [ "$ENVIRONMENT" = "production" ]; then
    echo ""
    echo "🚀 Шаг 7: Перезагрузка systemd сервиса..."
    if sudo systemctl is-active --quiet $SYSTEMD_SERVICE; then
        echo "  Останавливаю $SYSTEMD_SERVICE..."
        sudo systemctl stop $SYSTEMD_SERVICE
        sleep 2
    fi
    
    echo "  Запускаю $SYSTEMD_SERVICE..."
    sudo systemctl start $SYSTEMD_SERVICE
    sleep 2
    
    if sudo systemctl is-active --quiet $SYSTEMD_SERVICE; then
        echo "✓ $SYSTEMD_SERVICE запущен успешно"
    else
        echo "❌ Ошибка при запуске $SYSTEMD_SERVICE!"
        echo "  Лог:"
        sudo systemctl status $SYSTEMD_SERVICE || true
        exit 1
    fi
else
    echo ""
    echo "💻 Development режим: сервис не перезагружен"
    echo "  Запусти вручную:"
    echo "  $VENV_DIR/bin/python3 -m uvicorn main:app --reload --host 0.0.0.0 --port 8000"
fi

# 8. Финальная проверка
echo ""
echo "✓ Шаг 8: Финальная проверка здоровья..."
sleep 3
python3 << 'HEALTH_CHECK'
import urllib.request
import json
import sys

try:
    response = urllib.request.urlopen('http://localhost:8000/health', timeout=5)
    data = json.loads(response.read())
    
    connected_routers = sum(1 for r in data.get('routers', {}).values() if r.get('connectivity'))
    total_routers = len(data.get('routers', {}))
    
    print(f"  WiFi-Pay сервис работает: {connected_routers}/{total_routers} роутеров доступны")
    
    if connected_routers == 0:
        print("  ⚠️  ВНИМАНИЕ: Ни один роутер не доступен!")
        sys.exit(1)
except Exception as e:
    print(f"  ⚠️  Не удалось проверить здоровье: {str(e)[:100]}")
    print("     Это OK если сервис еще стартует...")

sys.exit(0)
HEALTH_CHECK

echo ""
echo "╔════════════════════════════════════════════════════════════╗"
echo "║  ✅ Развертывание успешно завершено!                      ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""
echo "🔗 Полезные ссылки:"
echo "   - Портал:     http://wifi-pay.kz/"
echo "   - Health:     http://wifi-pay.kz/health"
echo "   - Логи:       sudo journalctl -u wifi-pay -f"
echo "   - Статус:     sudo systemctl status wifi-pay"
echo ""
echo "ℹ️  Чтобы развернуть обновления:"
echo "   cd ~/wi-fi-pay && git pull && ./deploy.sh production"
echo ""
