#!/usr/bin/env python3
"""Управление белым списком водителей (таблица driver_phones).

Позволяет посмотреть список номеров водителей по роутерам и удалить номер
(с отзывом бессрочного доступа на самом MikroTik, если MAC уже был определён).

Использование:
  python manage_drivers.py                      # интерактивное меню
  python manage_drivers.py list                  # список всех водителей по роутерам
  python manage_drivers.py list astana_03         # список водителей конкретного роутера
  python manage_drivers.py delete +7 777 000 00 00  # удалить номер (телефон одним аргументом
                                                       # или несколькими — склеятся пробелом)
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Windows-консоли (cmd/PowerShell) часто не в UTF-8 по умолчанию — без этого
# кириллица в выводе превращается в кракозябры.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "gateway.db"
ROUTERS_CONFIG_PATH = BASE_DIR / "routers_config.json"

ROUTER_USER_ENV = (os.getenv("ROUTER_USER") or "").strip()
ROUTER_PASS_ENV = (os.getenv("ROUTER_PASS") or "").strip()


def _normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if len(digits) != 11 or not digits.startswith("7"):
        return ""
    return digits


def load_routers_config() -> dict:
    if not ROUTERS_CONFIG_PATH.exists():
        return {}
    with open(ROUTERS_CONFIG_PATH, encoding="utf-8-sig") as f:
        routers = json.load(f)
    result = {}
    for r in routers:
        if ROUTER_USER_ENV:
            r["user"] = ROUTER_USER_ENV
        if ROUTER_PASS_ENV:
            r["pass"] = ROUTER_PASS_ENV
        result[r["id"]] = r
    return result


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def fetch_drivers(router_id: str | None = None) -> list[sqlite3.Row]:
    conn = get_conn()
    try:
        if router_id:
            rows = conn.execute(
                "SELECT phone, router_id, mac_address, note, updated_at FROM driver_phones "
                "WHERE router_id=? ORDER BY updated_at DESC",
                (router_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT phone, router_id, mac_address, note, updated_at FROM driver_phones "
                "ORDER BY router_id, updated_at DESC"
            ).fetchall()
    finally:
        conn.close()
    return rows


def format_phone(phone: str) -> str:
    if len(phone) == 11:
        return f"+{phone[0]} {phone[1:4]} {phone[4:7]} {phone[7:9]} {phone[9:11]}"
    return phone


def print_drivers(router_id: str | None = None) -> None:
    rows = fetch_drivers(router_id)
    if not rows:
        print("Водителей пока нет" + (f" на {router_id}" if router_id else ""))
        return

    by_router: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_router.setdefault(row["router_id"], []).append(row)

    for rid in sorted(by_router.keys()):
        entries = by_router[rid]
        print(f"\n{rid} ({len(entries)}/5):")
        for row in entries:
            mac = row["mac_address"] or "не определён (ждёт /restore_access)"
            note = f" — {row['note']}" if row["note"] else ""
            print(f"  {format_phone(row['phone'])}  [{mac}]{note}")


def revoke_on_router(router_id: str, mac: str, routers_config: dict) -> bool:
    """Удаляет постоянный PAID_ ip-binding водителя на MikroTik. Возвращает True при успехе
    (в т.ч. если роутер недоступен — тогда просто предупреждаем и продолжаем удаление в БД,
    как и с самим гарантом доступа: сначала записываем список, доступ подтягивается позже)."""
    config = routers_config.get(router_id)
    if not config:
        print(f"⚠️ Роутер {router_id} не найден в routers_config.json — пропускаю отзыв на MikroTik")
        return False

    import routeros_api

    connection = None
    try:
        connection = routeros_api.RouterOsApiPool(
            config["ip"],
            username=config["user"],
            password=config["pass"],
            port=config.get("port", 8728),
            plaintext_login=True,
        )
        api = connection.get_api()
        binding = api.get_resource("/ip/hotspot/ip-binding")
        removed = 0
        for row in binding.call("print", queries={"mac-address": mac}):
            comment = row.get("comment") or ""
            if comment.startswith("PAID_"):
                binding.call("remove", arguments={".id": row.get("id") or row.get(".id")})
                removed += 1
        print(f"✓ Отозвано биндингов на {router_id}: {removed}")
        return True
    except Exception as e:
        print(f"⚠️ Не удалось подключиться к {router_id}: {str(e)[:150]}")
        return False
    finally:
        if connection:
            try:
                connection.disconnect()
            except Exception:
                pass


def delete_driver(phone_raw: str) -> None:
    phone = _normalize_phone(phone_raw)
    if not phone:
        print(f"Некорректный номер телефона: {phone_raw}")
        return

    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT phone, router_id, mac_address, note FROM driver_phones WHERE phone=?",
            (phone,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        print(f"Номер {format_phone(phone)} не найден в списке водителей")
        return

    print(f"Найден: {format_phone(phone)} на {row['router_id']} "
          f"(MAC: {row['mac_address'] or 'не определён'}, комментарий: {row['note'] or '-'})")

    if row["mac_address"]:
        routers_config = load_routers_config()
        revoke_on_router(row["router_id"], row["mac_address"], routers_config)
    else:
        print("MAC не был определён — на MikroTik нечего отзывать")

    conn = get_conn()
    try:
        conn.execute("DELETE FROM driver_phones WHERE phone=?", (phone,))
        conn.commit()
    finally:
        conn.close()
    print(f"Удалён из списка водителей: {format_phone(phone)}")


def interactive_menu() -> None:
    while True:
        print("\n=== WiFi-Pay Driver Manager ===")
        print("1) Показать список водителей по роутерам")
        print("2) Показать водителей конкретного роутера")
        print("3) Удалить водителя по номеру телефона")
        print("4) Выйти")

        choice = input("Выберите действие: ").strip()

        if choice == "1":
            print_drivers()
        elif choice == "2":
            router_id = input("ID роутера (например astana_03): ").strip()
            print_drivers(router_id)
        elif choice == "3":
            phone = input("Номер телефона водителя: ").strip()
            confirm = input(f"Удалить {phone} и отозвать доступ? (yes/no): ").strip().lower()
            if confirm in ("yes", "y", "да"):
                delete_driver(phone)
            else:
                print("Отменено")
        elif choice == "4":
            print("Выход")
            return
        else:
            print("Неизвестный пункт меню")


def main() -> None:
    if not DB_PATH.exists():
        print(f"БД не найдена: {DB_PATH}")
        sys.exit(1)

    args = sys.argv[1:]
    if not args:
        interactive_menu()
        return

    cmd = args[0].lower()
    if cmd == "list":
        router_id = args[1] if len(args) > 1 else None
        print_drivers(router_id)
    elif cmd == "delete":
        if len(args) < 2:
            print("Использование: python manage_drivers.py delete <номер телефона>")
            sys.exit(1)
        delete_driver(" ".join(args[1:]))
    else:
        print(f"Неизвестная команда: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
