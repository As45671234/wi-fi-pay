# 🌐 WiFi-Pay - Captive Portal Solution

Платная система доступа в Wi-Fi с интеграцией MikroTik роутеров и платежной системой FreedomPay.

---

## 🚀 Quick Start (5 минут)

### На VPS Ubuntu 22.04+:

```bash
# 1. Клонируй репозиторий
mkdir -p ~/wi-fi-pay && cd ~/wi-fi-pay
git clone https://github.com/As45671234/wi-fi-pay.git .

# 2. Установи systemd сервис
sudo cp wifi-pay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable wifi-pay

# 3. Запусти развертывание
chmod +x deploy.sh diagnose.sh
./deploy.sh production

# 4. Проверь здоровье
curl http://localhost:8000/health | python3 -m json.tool
```

**Готово!** 🎉

Система работает на `http://wifi-pay.kz` и тебе просто нужно убедиться что все роутеры доступны.

---

## 📋 Что это?

**WiFi-Pay** - это полный решение для продажи доступа в Wi-Fi:

- 📱 **Captive Portal** — автоматическое перенаправление на портал при подключении
- 💳 **Платежи** — интеграция с FreedomPay (Казахстан)
- ⏱️ **Trial доступ** — 15 минут бесплатно (1 раз в 24 часа на устройство)
- 🔐 **Безопасность** — HMAC подписи, MAC-фильтрация, Scheduler очистка
- 📊 **Мониторинг** — /health, /debug endpoints для диагностики
- 🔄 **iOS/Android** — специальная логика для разных ОС

---

## 🏗️ Архитектура

```
┌─────────────────────────────────────────────────────────────────┐
│                     ПОЛЬЗОВАТЕЛЬ (мобилка)                      │
│              подключается к Wi-Fi → Captive Portal              │
└────────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────────┐
│        MikroTik Роутер (astana_01, astana_02, etc)              │
│        ├─ Hotspot (Captive Portal)                              │
│        ├─ IP-Bindings (MAC фильтрация)                          │
│        ├─ Schedulers (автоочистка сессий)                       │
│        └─ WireGuard (10.0.0.X/24)                               │
└──────────────────────┬──────────────────────────────────────────┘
                       │ WireGuard Tunnel
                       ↓
┌─────────────────────────────────────────────────────────────────┐
│               VPS Backend (FastAPI + uvicorn)                   │
│        ├─ /tariffs → выбор тарифов                              │
│        ├─ /start_payment → платежный процесс                    │
│        ├─ /payment_result ← callback от банка                   │
│        ├─ /get_free_trial → 15 мин бесплатно                    │
│        ├─ /health → диагностика                                 │
│        └─ /debug → подробная диагностика MAC-адреса             │
└─────────────────────────────────────────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────────────────┐
│                   FreedomPay API (Платежи)                      │
│              https://api.freedompay.kz/payment.php              │
└─────────────────────────────────────────────────────────────────┘
```

---

## 📁 Файлы и папки

```
wifi-pay/
├─ main.py                    # Основной сервер (FastAPI)
├─ routers_config.json        # Конфиг роутеров
├─ gateway.db                 # SQLite база (история платежей)
├─ requirements.txt           # Python зависимости
├─ deploy.sh                  # Скрипт развертывания ⭐
├─ diagnose.sh               # Скрипт диагностики ⭐
├─ wifi-pay.service          # Systemd сервис
├─ DEPLOYMENT.md             # Подробный гайд по deploy ⭐
│
├─ templates/                # HTML шаблоны (Jinja2)
│  ├─ welcome.html           # Первая страница
│  ├─ index.html             # Выбор тарифов
│  ├─ success.html           # Подтверждение оплаты
│  ├─ payment_form.html      # Платежный виджет
│  └─ (другие инфо-страницы)
│
├─ login/                    # Старые файлы (не используется)
├─ доки/                     # Документация по роутерам
│  └─ Инструкция_настройки_роутеров_WiFiPay.txt
│
└─ Bash утилиты:
   ├─ list_clients.sh        # Список активных клиентов
   ├─ check_status.sh        # Статус конкретного MAC
   ├─ clear_device.sh        # Удалить доступ MAC-адреса
   └─ run                    # Быстрый старт
```

---

## 🔧 Постоянное управление

### Проверить статус:

```bash
sudo systemctl status wifi-pay
```

### Смотреть логи гживую:

```bash
sudo journalctl -u wifi-pay -f
```

### Искать ошибки в логах:

```bash
sudo journalctl -u wifi-pay -n 200 | grep -i error
```

### Перезагрузить сервис:

```bash
sudo systemctl restart wifi-pay
```

### Запустить диагностику:

```bash
./diagnose.sh
```

---

## 📊 API Endpoints

### Для пользователей:

- `GET /` — Welcome страница
- `GET /tariffs?mac=XX:XX:XX&router_id=astana_01` — Выбор тарифов
- `GET /activate_welcome?mac=XX:XX:XX&router_id=astana_01` — Активация PAY_WINDOW
- `GET /start_payment?amount=500&mac=XX:XX:XX&router_id=astana_01` — Инициирование платежа
- `POST /payment_result` — Callback от FreedomPay (используется банком)
- `POST /get_free_trial` — Trial доступ на 15 минут
- `GET /success?mac=XX&router_id=astana_01&minutes=1440&amount=500` — Финальная страница

### Для диагностики:

- `GET /health` — Проверка здоровья всех роутеров
- `GET /debug?mac=XX:XX:XX&router_id=astana_01` — Подробная диагностика MAC-адреса

### Инфо-страницы:

- `GET /payment_methods` — Способы оплаты
- `GET /offer` — Оферта (договор)
- `GET /privacy` — Приватность

---

## 🔑 Конфигурация

### `routers_config.json`

```json
[
  {
    "id": "astana_01",
    "ip": "10.0.0.2",
    "user": "admin",
    "pass": "your_password_here",
    "portal_probe_url": "http://captive.apple.com/hotspot-detect.html"
  },
  {
    "id": "astana_02",
    "ip": "10.0.0.3",
    "user": "admin",
    "pass": "your_password_here",
    "portal_probe_url": "http://captive.apple.com/hotspot-detect.html"
   },
   {
      "id": "astana_07",
      "ip": "10.0.0.8",
      "user": "admin",
      "pass": "your_password_here",
      "portal_probe_url": "http://captive.apple.com/hotspot-detect.html"
   },
   {
      "id": "astana_08",
      "ip": "10.0.0.9",
      "user": "admin",
      "pass": "your_password_here",
      "portal_probe_url": "http://captive.apple.com/hotspot-detect.html"
   },
   {
      "id": "astana_09",
      "ip": "10.0.0.10",
      "user": "admin",
      "pass": "your_password_here",
      "portal_probe_url": "http://captive.apple.com/hotspot-detect.html"
     },
     {
        "id": "astana_10",
        "ip": "10.0.0.11",
        "user": "admin",
        "pass": "your_password_here",
        "portal_probe_url": "http://captive.apple.com/hotspot-detect.html"
     },
     {
        "id": "astana_11",
        "ip": "10.0.0.12",
        "user": "admin",
        "pass": "your_password_here",
        "portal_probe_url": "http://captive.apple.com/hotspot-detect.html"
     },
     {
        "id": "astana_12",
        "ip": "10.0.0.13",
        "user": "admin",
        "pass": "your_password_here",
        "portal_probe_url": "http://captive.apple.com/hotspot-detect.html"
     },
     {
        "id": "astana_13",
        "ip": "10.0.0.14",
        "user": "admin",
        "pass": "your_password_here",
        "portal_probe_url": "http://captive.apple.com/hotspot-detect.html"
     },
     {
        "id": "astana_14",
        "ip": "10.0.0.15",
        "user": "admin",
        "pass": "your_password_here",
        "portal_probe_url": "http://captive.apple.com/hotspot-detect.html"
  }
]
```

### Переменные в `main.py`

```python
MERCHANT_ID = "581983"           # FreedomPay ID
SECRET_KEY = "PMwioQEEE..."      # Подпись платежей
TRIAL_TOKEN_TTL = 300            # 5 минут жизнь подписи
TRIAL_RATE_LIMIT = 6 в 10 мин    # Защита от перебора
```

---

## 🌊 Жизненный цикл клиента

### 1️⃣ Подключение к Wi-Fi

```
iPhone подключается к Wifi-Pay-01 (открытая сеть, без пароля)
              ↓
Captive Portal перенаправляет на http://wifi-pay.kz/
```

### 2️⃣ Welcome страница

```
Клиент видит: "Выберите тариф и оплатите доступ"
              ↓
Нажимает: "ВЫБРАТЬ ТАРИФ"
              ↓
Срабатывает: /activate_welcome
  - iPhone: PAY_WINDOW на 90 сек
  - Android: редирект на /tariffs БЕЗ доступа
  - Другое: PAY_WINDOW на 3 минуты
```

### 3️⃣ Страница тарифов

```
Две опции:
  1. "15 мин бесплатно" (0 ₸)           → /get_free_trial
       + Проверка: не использовал последние 24ч?
       + Активирует TRIAL на 15 минут
  
   2. "Безлимит на день" (500 ₸)         → /start_payment
       + Активирует PAY_WINDOW на 3 мин
       + Редирект на FreedomPay
```

### 4️⃣ Оплата

```
FreedomPay платежный виджет
    ↓
Клиент вводит данные карты
    ↓
Callback на /payment_result
    ↓
Верификация подписи
    ↓
Активирует PAID на 24 часа
    ↓
Редирект на /success
```

### 5️⃣ Успех

```
Success страница с таймером 24:00:00
  - Показывает сумму и тариф
  - Обратный отсчет до истечения
  - Кнопка "Готово" закрывает портал
```

---

## 🛡️ Безопасность

### HMAC подписи

```python
# Trial подпись (5 мин TTL)
signature = HMAC-SHA256(mac|router_id|timestamp, SECRET_KEY)

# Платежная подпись
payment_sig = MD5(script_name;param1;param2;...;SECRET_KEY)
```

### Rate Limiting

```python
# Trial: 6 попыток за 10 минут на IP-адрес
# Защита от автоматических перебор атак
```

### Device Tracking

```python
# Кукка wf_device_id на 365 дней
# Предотвращает повторный trial
```

### MAC Фильтрация

```python
# Только валидные MAC-адреса формата XX:XX:XX:XX:XX:XX
# Проверка перед каждой операцией
```

---

## 📞 Troubleshooting

### ❌ "Portал не открывается"

1. Проверь WireGuard:
   ```bash
   sudo wg show
   ```

2. Ping роутер:
   ```bash
   ping 10.0.0.2
   ```

3. Проверь Hotspot на MikroTik:
   ```bash
   /ip hotspot settings print
   ```

### ❌ "Уже не подходит интернет после оплаты"

1. Проверь scheduler на роутере:
   ```bash
   ./check_status.sh XX:XX:XX:XX:XX:XX
   ```

2. Посмотри логи:
   ```bash
   sudo journalctl -u wifi-pay -f | grep MAC
   ```

3. Диагностируй MAC:
   ```bash
   curl "http://localhost:8000/debug?mac=XX:XX:XX:XX:XX:XX&router_id=astana_01"
   ```

---

## 📝 Обновления

### Развернуть обновления:

```bash
cd ~/wi-fi-pay
git pull origin main
./deploy.sh production
```

Скрипт автоматически:
- ✓ Проверит зависимости
- ✓ Проверит подключение к роутерам
- ✓ Проверит синтаксис кода
- ✓ Перезагрузит сервис
- ✓ Проверит здоровье

---

## 📚 Дополнительно

- [DEPLOYMENT.md](DEPLOYMENT.md) — Подробный гайд по deployment и troubleshooting
- [доки/Инструкция_настройки_роутеров_WiFiPay.txt](доки/Инструкция_настройки_роутеров_WiFiPay.txt) — Как настроить MikroTik роутер
- `./diagnose.sh` — Скрипт для диагностики проблем

---

## 📄 Лицензия

Закрытый проект. © 2026 WiFi-Pay

---

## 🎯 Поддержка

Если нужна помощь:

1. Запусти диагностику:
   ```bash
   ./diagnose.sh
   ```

2. Посмотри логи:
   ```bash
   sudo journalctl -u wifi-pay -n 200
   ```

3. Проверь `/health` endpoint:
   ```bash
   curl http://localhost:8000/health | python3 -m json.tool
   ```

4. Диагностируй конкретный MAC:
   ```bash
   curl "http://localhost:8000/debug?mac=XX:XX:XX:XX:XX:XX&router_id=astana_01"
   ```

---

**Happy selling! 🚀**
