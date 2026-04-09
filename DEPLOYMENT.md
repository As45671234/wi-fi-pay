# WiFi-Pay Production Deployment Guide

## 🚀 Быстрое развертывание (Recommended)

### На VPS:

```bash
cd ~/wi-fi-pay
git pull
./deploy.sh production
```

**Скрипт автоматически:**
- ✓ Проверит Python3, Git, зависимости
- ✓ Создаст виртуальное окружение
- ✓ Установит requirements.txt
- ✓ **Проверит подключение ко ВСЕМ роутерам**
- ✓ Проверит синтаксис main.py
- ✓ Перезагрузит systemd сервис
- ✓ Проверит здоровье сервиса

---

## 📋 Первоначальная настройка (ONE TIME)

### 1. Подготовить директорию:

```bash
mkdir -p ~/wi-fi-pay
cd ~/wi-fi-pay
git clone https://github.com/As45671234/wi-fi-pay.git .
```

### 2. Установить systemd сервис:

```bash
sudo cp wifi-pay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable wifi-pay
```

### 3. Создать виртуальное окружение:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4. Проверить конфиг routers_config.json:

```bash
cat routers_config.json
```

Убедиться что IPs правильные и все роутеры перечислены.

### 5. Запустить первый раз:

```bash
./deploy.sh production
```

---

## 🔄 Обновления в Production

Каждый раз когда нужно развернуть обновления:

```bash
cd ~/wi-fi-pay
git pull
./deploy.sh production
```

---

## 📊 Мониторинг

### Проверить статус:

```bash
sudo systemctl status wifi-pay
```

### Смотреть логи в реальном времени:

```bash
sudo journalctl -u wifi-pay -f
```

### Смотреть последние 100 строк логов:

```bash
sudo journalctl -u wifi-pay -n 100
```

### Проверить здоровье сервиса:

```bash
curl http://localhost:8000/health | python3 -m json.tool
```

**Вывод должен быть:**
```json
{
  "timestamp": "2026-03-26T15:30:45+06:00",
  "routers": {
    "astana_01": {"ip": "10.0.0.2", "connectivity": true, "hotspot_enabled": true},
    "astana_02": {"ip": "10.0.0.3", "connectivity": true, "hotspot_enabled": true},
    ...
  }
}
```

---

## 🔍 Диагностика конкретного MAC:

```bash
curl "http://localhost:8000/debug?mac=82:10:24:99:CE:76&router_id=astana_01" | python3 -m json.tool
```

Показывает:
- Все биндинги для MAC
- Активные сессии
- Hotspot юзеры
- Scheduler задачи

---

## 🛠️ Ручные команды

### Перезагрузить сервис:

```bash
sudo systemctl restart wifi-pay
```

### Остановить:

```bash
sudo systemctl stop wifi-pay
```

### Запустить:

```bash
sudo systemctl start wifi-pay
```

### Посмотреть логи после перезагрузки (10 сек):

```bash
sudo systemctl restart wifi-pay && sleep 10 && sudo journalctl -u wifi-pay -n 50
```

---

## 🐛 Troubleshooting

### ❌ "Ни один роутер не доступен"

Проверить:
1. **WireGuard туннель:**
   ```bash
   sudo wg show
   ```
   Должен быть `wg0` с IP 10.0.0.1

2. **ping до роутеров:**
   ```bash
   ping -c 1 10.0.0.2
   ping -c 1 10.0.0.3
   ```

3. **Firewall:**
   ```bash
   sudo ufw status
   sudo ufw allow 8728  # MikroTik API port
   ```

### ❌ "systemctl restart wifi-pay" зависает

```bash
# Принудительно убить процесс
sudo pkill -9 -f "uvicorn main:app"
# Запустить заново
sudo systemctl start wifi-pay
```

### ❌ Порт 8000 занят

```bash
# Найти что слушает на 8000
sudo lsof -i :8000
# Убить процесс
sudo kill -9 <PID>
```

---

## 📈 Performance Tips

### Увеличить worker'ы если много перегруженности:

В `wifi-pay.service` изменить:
```bash
ExecStart=/home/ubuntu/wi-fi-pay/venv/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
```

### Мониторить использование ресурсов:

```bash
watch -n 1 'ps aux | grep "[u]vicorn"'
```

---

## 🔐 Security Notes

- ✓ routers_config.json содержит пароли - **НЕ коммитить в Git!**
- ✓ Используй SSH туннель для управления
- ✓ Установи fail2ban для защиты
- ✓ Ограничь доступ к /health и /debug от определенных IP

---

## 📝 Логирование

Все события логируются с префиксом:
```
[activate_welcome] - Welcome страница / активация доступа
[start_payment]    - Инициирование платежа
[payment_result]   - Callback от банка
[get_free_trial]   - Бесплатный trial доступ
[VERIFY]           - Проверка что доступ реально активирован
```

Ищи в логах **🔍** для ссылок на диагностику:
```bash
sudo journalctl -u wifi-pay -f | grep "🔍"
```

---

## 🎯 Автоматизация (Optional)

### Каждый час проверять здоровье:

```bash
crontab -e
```

Добавить:
```cron
0 * * * * curl -s http://localhost:8000/health > /dev/null || sudo systemctl restart wifi-pay
```

### Еженедельное резервное копирование БД:

```bash
0 2 * * 0 cp ~/wi-fi-pay/gateway.db ~/wi-fi-pay/backups/gateway.db.$(date +\%Y\%m\%d)
```

---

## 📞 Support

Если что-то сломалось:

1. **Проверь логи:**
   ```bash
   sudo journalctl -u wifi-pay -n 200 | grep -i error
   ```

2. **Запусти `/health`:**
   ```bash
   curl http://localhost:8000/health
   ```

3. **Диагностируй конкретный MAC:**
   ```bash
   curl "http://localhost:8000/debug?mac=XX:XX:XX:XX:XX:XX&router_id=astana_01"
   ```

4. **Посмотри активные клиенты:**
   ```bash
   ROUTER_IPS=10.0.0.2,10.0.0.3,10.0.0.4,10.0.0.5,10.0.0.6,10.0.0.7,10.0.0.8,10.0.0.9,10.0.0.10 ./list_clients.sh
   ```

---
