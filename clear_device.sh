#!/bin/bash
# Скрипт для очистки истории устройства

MAC="${1:-22:1D:C8:99:FE:B0}"
ROUTER_IP="10.0.0.2"
ROUTER_USER="admin"
SSH_KEY="$HOME/.ssh/mikrotik_key"

echo "🧹 Очистка истории для MAC: $MAC"

# Проверка SSH ключа
if [ ! -f "$SSH_KEY" ]; then
    echo "❌ SSH ключ не найден: $SSH_KEY"
    exit 1
fi

# Очистка БД
echo "Удаление записей из БД..."
sqlite3 gateway.db "DELETE FROM orders WHERE mac_address = '$MAC';"
echo "✓ БД очищена"

# Очистка MikroTik
echo "Очистка MikroTik..."
MAC_HEX=$(echo $MAC | tr -d ':')

ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no ${ROUTER_USER}@${ROUTER_IP} << EOF
/ip hotspot user remove [find comment~"$MAC"]
/ip hotspot ip-binding remove [find mac-address="$MAC"]
/ip hotspot active remove [find mac-address="$MAC"]
/system scheduler remove [find name~"$MAC_HEX"]
EOF

echo "✓ MikroTik очищен"
echo "✅ Устройство $MAC готово к повторному тестированию!"
