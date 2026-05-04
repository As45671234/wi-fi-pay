# 🚀 WiFi-Pay Quick Start Guide

## На твоем ПК (Windows/Mac/Linux)

### 1. Git Push изменений

```bash
cd C:\Проекты\wifi-pay
git add .
git commit -m "Add deploy scripts, health check, debug endpoints"
git push origin main
```

### 2. Проверь что все файлы в GitHub

```bash
https://github.com/As45671234/wi-fi-pay
```

Должен быть:
```
✓ main.py (обновленный с диагностикой)
✓ deploy.sh (новый - автодеплой)
✓ diagnose.sh (новый - диагностика)
✓ wifi-pay.service (новый - systemd сервис)
✓ requirements.txt (новый)
✓ README.md (обновленный)
✓ DEPLOYMENT.md (новый - подробный гайд)
```

---

## На VPS (Ubuntu 22.04)

### 1. SSH подключиться

```bash
ssh ubuntu@46.247.42.123
```

### 2. Обновить репозиторий

```bash
cd ~/wi-fi-pay
git pull origin main
```

### 3. Дать права на скрипты

```bash
chmod +x deploy.sh diagnose.sh
```

### 4. Первоначальная установка (ОДНОРАЗОВО)

```bash
# Установить systemd сервис
sudo cp wifi-pay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable wifi-pay

# Проверить conifg
cat routers_config.json
```

### 5. Запустить развертывание

```bash
./deploy.sh production
```

**Скрипт автоматически:**
- ✓ Создаст виртуальное окружение
- ✓ Установит зависимости
- ✓ **💥 Проверит подключение ко ВСЕМ роутерам**
- ✓ Проверит синтаксис main.py
- ✓ Перезагрузит systemd сервис
- ✓ Проверит здоровье

**OUTPUT будет примерно:**

```
╔════════════════════════════════════════════════════════════╗
║        WiFi-Pay Development Deploy Script                 ║
║        Environment: production                            ║
╚════════════════════════════════════════════════════════════╝

📦 Шаг 1: Проверка зависимостей...
✓ Python3: Python 3.10.13

🐍 Шаг 2: Подготовка виртуального окружения...
✓ venv уже существует

📚 Шаг 3: Установка зависимостей Python...
✓ Стандартные зависимости установлены

⚙️  Шаг 4: Проверка конфигурации...
✓ Найдено 4 роутеров в конфигурации

🔌 Шаг 5: Проверка подключения к роутерам...
  Проверяю astana_01 (10.0.0.2)... ✓ OK (Identity: MikroTik RouterOS 6.48.3)
  Проверяю astana_02 (10.0.0.3)... ✓ OK (Identity: MikroTik RouterOS 6.48.3)
  Проверяю astana_03 (10.0.0.4)... ❌ Timeout (роутер не отвечает)
  Проверяю astana_04 (10.0.0.5)... ✓ OK (Identity: MikroTik RouterOS 6.48.3)

  Результат: 3 OK, 1 ОШИБОК

✓ Шаг 6: Проверка синтаксиса Python...
✓ Синтаксис OK

🚀 Шаг 7: Перезагрузка systemd сервиса...
✓ wifi-pay запущен успешно

✓ Шаг 8: Финальная проверка здоровья...
  WiFi-Pay сервис работает: 3/4 роутеров доступны

╔════════════════════════════════════════════════════════════╗
║  ✅ Развертывание успешно завершено!                      ║
╚════════════════════════════════════════════════════════════╝
```

---

## ✅ Проверки после развертывания

### 1. Проверить статус сервиса

```bash
sudo systemctl status wifi-pay
```

Должно быть: `Active: active (running)`

### 2. Проверить логи

```bash
sudo journalctl -u wifi-pay -n 50
```

Должно быть что-то типа:
```
[activate_welcome] User-Agent: Mozilla/5.0...
[start_payment] ✓ PAY_WINDOW активирован
```

### 3. Проверить /health endpoint

```bash
curl http://localhost:8000/health | python3 -m json.tool
```

Вывод:
```json
{
  "timestamp": "2026-03-26T15:30:00+06:00",
  "routers": {
    "astana_01": {
      "ip": "10.0.0.2",
      "connectivity": true,
      "hotspot_enabled": true,
      "details": "Identity: MikroTik..."
    },
    "astana_02": {
      "ip": "10.0.0.3",
      "connectivity": true,
      "hotspot_enabled": true,
      "details": "Identity: MikroTik..."
    },
    ...
  }
}
```

### 4. Запустить диагностику

```bash
cd ~/wi-fi-pay && ./diagnose.sh
```

Вывод покажет всё состояние системы.

---

## 🔍 Если что-то не работает

### ❌ "Роутеры не доступны"

```bash
# Проверить WireGuard
sudo wg show

# Ping роутеры
ping -c 1 10.0.0.2
ping -c 1 10.0.0.3

# Firewall
sudo ufw status

# Открыть MikroTik API port если нужно
sudo ufw allow 8728
```

### ❌ "Сервис не запускается"

```bash
# Смотреть ошибку
sudo systemctl status wifi-pay -l

# Логи
sudo journalctl -u wifi-pay -n 200 | grep -i error

# Пересохить и возможно перезагрузиться
sudo systemctl restart wifi-pay
```

### ❌ "Portал не открывается"

```bash
# Проверить что сервис слушает на 8000
sudo lsof -i :8000

# Тест локально
curl http://localhost:8000/

# Проверить DNS/https if используешь домен
curl -v http://wifi-pay.kz/
```

---

## 🔄 Постоянное обновление

Каждый раз когда нужны обновления:

### На PC:

```bash
cd C:\Проекты\wifi-pay
git add .
git commit -m "Update: your message"
git push
```

### На VPS:

```bash
cd ~/wi-fi-pay
git pull
./deploy.sh production
```

---

## 📊 Мониторинг (Optional)

### Смотреть логи гживую

```bash
sudo journalctl -u wifi-pay -f
```

### Искать конкретное событие

```bash
# Все платежи
sudo journalctl -u wifi-pay | grep "payment_result"

# Все ошибки
sudo journalctl -u wifi-pay | grep -i error

# Trial доступы
sudo journalctl -u wifi-pay | grep "get_free_trial"
```

### Проверить клиентов

```bash
# На каком роутере есть клиенты
ROUTER_IPS=10.0.0.2,10.0.0.3,10.0.0.4,10.0.0.5,10.0.0.6,10.0.0.7,10.0.0.8,10.0.0.9,10.0.0.10,10.0.0.11,10.0.0.12,10.0.0.13,10.0.0.14,10.0.0.15,10.0.0.16,10.0.0.17 ./list_clients.sh

# Статус конкретного MAC
./check_status.sh XX:XX:XX:XX:XX:XX

# Удалить доступ MAC
./clear_device.sh XX:XX:XX:XX:XX:XX
```

---

## 📈 Оптимизация (Optional)

### Увеличить worker'ы если нагруз велика

```bash
sudo systemctl edit wifi-pay
```

Изменить:
```ini
ExecStart=/home/ubuntu/wi-fi-pay/venv/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
```

Потом:
```bash
sudo systemctl restart wifi-pay
```

---

## 🎯 Что дальше?

1. ✅ Протестировать подключение с айфона/андроида
2. ✅ Проверить бесплатный trial
3. ✅ Проверить платежный процесс (тестовый платеж в FreedomPay)
4. ✅ Убедиться что после оплаты интернет работает 24ч
5. ✅ Настроить SSL/TLS сертификат (Let's Encrypt)
6. ✅ Настроить DNS для wifi-pay.kz

---

## 📚 Дополнительно

- **DEPLOYMENT.md** — Подробный гайд с troubleshooting
- **README.md** — Общая информация о проекте
- **./deploy.sh** — Смотреть что делает скрипт
- **./diagnose.sh** — Диагностических инструмент

---

## 💡 Pro Tips

```bash
# Быстрая перезагрузка с проверкой логов
sudo systemctl restart wifi-pay && sleep 5 && sudo journalctl -u wifi-pay -n 20

# Смотреть ошибки последних 30мин
sudo journalctl -u wifi-pay --since "30 min ago" | grep -i error

# Тестировать доступность всех роутеров
for ip in 10.0.0.2 10.0.0.3 10.0.0.4 10.0.0.5 10.0.0.6 10.0.0.7 10.0.0.8 10.0.0.9 10.0.0.10 10.0.0.11 10.0.0.12 10.0.0.13 10.0.0.14 10.0.0.15 10.0.0.16 10.0.0.17; do echo "=== $ip ==="; ping -c 1 $ip || echo "FAIL"; done

# Проверить что PAY_WINDOW будет удаляться через 3 мин
curl "http://localhost:8000/debug?mac=XX:XX:XX:XX:XX:XX&router_id=astana_01" | jq '.schedulers'
```

---

**Готово! Система работает. 🚀**

Перейди на http://wifi-pay.kz/ и тестируй!

Если чтоты не понимаешь — читай DEPLOYMENT.md или запусти `./diagnose.sh`.
