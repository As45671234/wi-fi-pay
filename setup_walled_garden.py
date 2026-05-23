"""
setup_walled_garden.py — Добавляет VPS IP в walled-garden-ip на всех роутерах.

Запускать ОДИН РАЗ на VPS:
    python3 setup_walled_garden.py

После этого wifi-pay.kz будет доступен без PAY_WINDOW (HTTPS работает в CNA),
и iOS 36-секундный freeze полностью исчезнет.
"""
import json
import os
import socket
import sys
import urllib.request

import routeros_api


def _load_env(path: str = ".env") -> dict:
    """Загружаем .env вручную (без зависимостей)."""
    env = {}
    if not os.path.exists(path):
        return env
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def get_vps_ip() -> str:
    """Узнаём публичный IP этого сервера."""
    for url in [
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://icanhazip.com",
    ]:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.read().decode().strip()
        except Exception:
            continue
    raise RuntimeError("Не удалось определить публичный IP VPS")


def add_walled_garden(config: dict, vps_ip: str) -> bool:
    router_id = config["id"]
    ip = config["ip"]
    port = int(config.get("port", 8728))
    user = config.get("user") or "admin"
    password = config.get("pass") or ""

    print(f"  [{router_id}] подключаемся к {ip}:{port} ...", end=" ", flush=True)
    connection = None
    try:
        connection = routeros_api.RouterOsApiPool(
            ip, username=user, password=password,
            port=port, plaintext_login=True,
        )
        api = connection.get_api()
        wg = api.get_resource("/ip/hotspot/walled-garden-ip")

        # Проверяем, нет ли уже такой записи
        existing = wg.call("print", queries={"dst-address": f"{vps_ip}/32"})
        if existing:
            print(f"уже есть ✓")
            return True

        wg.call("add", arguments={
            "dst-address": f"{vps_ip}/32",
            "comment": "WiFiPay-portal",
        })
        print(f"добавлено ✓  ({vps_ip}/32)")
        return True

    except Exception as e:
        print(f"ОШИБКА: {e}")
        return False
    finally:
        if connection:
            try:
                connection.disconnect()
            except Exception:
                pass


def main():
    print("=== WiFiPay: настройка Walled Garden ===\n")

    # Загружаем учётные данные из .env (ROUTER_USER / ROUTER_PASS)
    env = _load_env()
    global_user = env.get("ROUTER_USER", "").strip()
    global_pass = env.get("ROUTER_PASS", "").strip()
    if global_user:
        print(f"Учётные данные из .env: user={global_user}")

    # Определяем IP VPS
    if len(sys.argv) > 1:
        vps_ip = sys.argv[1]
        print(f"VPS IP (из аргумента): {vps_ip}")
    else:
        print("Определяем публичный IP VPS...", end=" ")
        vps_ip = get_vps_ip()
        print(vps_ip)

    print(f"\nДобавляем {vps_ip}/32 в walled-garden-ip на всех роутерах:\n")

    with open("routers_config.json", encoding="utf-8-sig") as f:
        routers = json.load(f)

    ok = 0
    fail = 0
    for router in routers:
        # Применяем глобальные credentials из .env (приоритет над JSON)
        if global_user:
            router["user"] = global_user
        if global_pass:
            router["pass"] = global_pass
        if add_walled_garden(router, vps_ip):
            ok += 1
        else:
            fail += 1

    print(f"\nГотово: {ok} успешно, {fail} ошибок.")
    if fail == 0:
        print("\n✅ Walled Garden настроен на всех роутерах!")
        print("   Теперь iOS пользователи смогут открывать HTTPS wifi-pay.kz без задержки.")
    else:
        print("\n⚠️  Часть роутеров не настроена. Проверь подключение и повтори.")


if __name__ == "__main__":
    main()
