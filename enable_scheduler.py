#!/usr/bin/env python3
"""
enable_scheduler.py — Отправляет команду включения scheduler на роутеры.
После запуска нужно физически нажать кнопку Reset на каждом роутере (в течение 5 минут).

Запуск:
  python3 enable_scheduler.py astana_24 astana_25
  python3 enable_scheduler.py astana_24 astana_25 astana_26
"""

import os
import sys
import json
import logging

import routeros_api

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("enable_scheduler")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROUTERS_CONFIG_PATH = os.path.join(BASE_DIR, "routers_config.json")

ROUTER_USER = os.getenv("ROUTER_USER", "").strip()
ROUTER_PASS = os.getenv("ROUTER_PASS", "").strip()


def load_routers():
    with open(ROUTERS_CONFIG_PATH, encoding="utf-8-sig") as f:
        routers = json.load(f)
    result = {}
    for r in routers:
        cfg = dict(r)
        if ROUTER_USER:
            cfg["user"] = ROUTER_USER
        if ROUTER_PASS:
            cfg["pass"] = ROUTER_PASS
        result[r["id"]] = cfg
    return result


def send_device_mode_update(cfg, router_id):
    pool = None
    try:
        pool = routeros_api.RouterOsApiPool(
            cfg["ip"],
            username=cfg.get("user", ""),
            password=cfg.get("pass", ""),
            port=int(cfg.get("port", 8728)),
            plaintext_login=True,
        )
        api = pool.get_api()

        # Проверяем текущий device-mode
        dm = api.get_resource("/system/device-mode")
        current = dm.call("print")[0]
        log.info("[%s] device-mode сейчас: scheduler=%s", router_id, current.get("scheduler", "?"))

        # Отправляем команду — роутер запустит таймер ожидания кнопки
        dm.call("update", arguments={"scheduler": "yes"})
        log.info("[%s] ✅ Команда отправлена!", router_id)
        return True
    except Exception as e:
        log.error("[%s] ❌ Ошибка: %s", router_id, e)
        return False
    finally:
        if pool:
            try:
                pool.disconnect()
            except Exception:
                pass


def main():
    if len(sys.argv) < 2:
        print("Укажи роутеры: python3 enable_scheduler.py astana_24 astana_25")
        sys.exit(1)

    router_ids = sys.argv[1:]
    routers = load_routers()

    unknown = [r for r in router_ids if r not in routers]
    if unknown:
        log.error("Неизвестные роутеры: %s", unknown)
        sys.exit(1)

    success = []
    for rid in router_ids:
        if send_device_mode_update(routers[rid], rid):
            success.append(rid)

    if success:
        print()
        print("=" * 60)
        print("⚠️  ТЕПЕРЬ ФИЗИЧЕСКИ НАЖМИ КНОПКУ RESET НА РОУТЕРАХ:")
        for rid in success:
            print(f"   • {rid}  ({routers[rid]['ip']})")
        print()
        print("   Времени: ~5 минут. После нажатия роутер перезагрузится.")
        print("=" * 60)


if __name__ == "__main__":
    main()
