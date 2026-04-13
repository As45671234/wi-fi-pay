#!/usr/bin/env python3
"""Interactive tariff manager for WiFi-Pay.

Functions:
- add_tariff: add tariff by minutes + amount
- edit_tariff: edit existing tariff by index

Writes changes to tariffs_config.json.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TARIFFS_FILE = BASE_DIR / "tariffs_config.json"
INDEX_TEMPLATE_FILE = BASE_DIR / "templates" / "index.html"
FALLBACK_START = "    // fallback-tariffs:start"
FALLBACK_END = "    // fallback-tariffs:end"


def hours_word(hours: int) -> str:
    mod10 = hours % 10
    mod100 = hours % 100
    if mod10 == 1 and mod100 != 11:
        return "час"
    if mod10 in (2, 3, 4) and mod100 not in (12, 13, 14):
        return "часа"
    return "часов"


def default_title(minutes: int) -> str:
    if minutes >= 1440:
        return "До конца поездки"
    if minutes % 60 == 0 and minutes >= 60:
        h = minutes // 60
        return f"{h} {hours_word(h)} доступа"
    return f"{minutes} минут доступа"


def default_subtitle(minutes: int) -> str:
    if minutes >= 1440:
        return "Максимум комфорта"
    if minutes % 60 == 0 and minutes >= 60:
        h = minutes // 60
        return f"Доступ на {h} {hours_word(h)}"
    return f"Доступ на {minutes} минут"


def recalc_badges(tariffs: list[dict]) -> None:
    for t in tariffs:
        t["badge"] = ""
    if not tariffs:
        return
    tariffs[0]["badge"] = "Популярное"
    if len(tariffs) >= 3:
        tariffs[-1]["badge"] = "Выгодно!"


def normalize_and_sort(tariffs: list[dict]) -> list[dict]:
    clean = []
    seen_amounts = set()
    for t in tariffs:
        amount = int(t["amount"])
        minutes = int(t["minutes"])
        if amount <= 0 or minutes <= 0:
            continue
        if amount in seen_amounts:
            continue
        seen_amounts.add(amount)
        clean.append(
            {
                "amount": amount,
                "minutes": minutes,
                "title": (t.get("title") or default_title(minutes)).strip(),
                "subtitle": (t.get("subtitle") or default_subtitle(minutes)).strip(),
                "badge": (t.get("badge") or "").strip(),
            }
        )
    clean.sort(key=lambda x: x["amount"])
    recalc_badges(clean)
    return clean


def load_tariffs() -> list[dict]:
    if not TARIFFS_FILE.exists():
        return normalize_and_sort([])
    data = json.loads(TARIFFS_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("tariffs_config.json must contain a JSON array")
    return normalize_and_sort(data)


def save_tariffs(tariffs: list[dict]) -> None:
    normalized = normalize_and_sort(tariffs)
    TARIFFS_FILE.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    sync_index_template(normalized)


def sync_index_template(tariffs: list[dict]) -> None:
    if not INDEX_TEMPLATE_FILE.exists():
        return

    content = INDEX_TEMPLATE_FILE.read_text(encoding="utf-8")
    pattern = re.compile(
        r"^[ \t]*// fallback-tariffs:start[\s\S]*?^[ \t]*// fallback-tariffs:end",
        re.MULTILINE,
    )
    if not pattern.search(content):
        return

    replacement = (
        f"{FALLBACK_START}\n"
        f"    const fallbackTariffs = {json.dumps(tariffs, ensure_ascii=False, indent=2)};\n"
        f"{FALLBACK_END}"
    )
    updated = pattern.sub(replacement, content, count=1)
    INDEX_TEMPLATE_FILE.write_text(updated, encoding="utf-8")


def ask_int(prompt: str) -> int:
    while True:
        raw = input(prompt).strip()
        try:
            value = int(raw)
            if value <= 0:
                print("Значение должно быть > 0")
                continue
            return value
        except ValueError:
            print("Введите целое число")


def list_tariffs(tariffs: list[dict]) -> None:
    if not tariffs:
        print("Тарифов пока нет")
        return
    print("\nТекущие тарифы:")
    for i, t in enumerate(tariffs, start=1):
        badge = f" [{t['badge']}]" if t.get("badge") else ""
        print(f"{i}. {t['title']} — {t['minutes']} мин — {t['amount']} ₸{badge}")


def add_tariff(tariffs: list[dict]) -> bool:
    print("\nДобавление тарифа")
    minutes = ask_int("Время доступа (в минутах): ")
    amount = ask_int("Цена (в тенге): ")

    if any(int(t["amount"]) == amount for t in tariffs):
        print(f"Тариф с ценой {amount} ₸ уже существует")
        return False

    tariffs.append(
        {
            "minutes": minutes,
            "amount": amount,
            "title": default_title(minutes),
            "subtitle": default_subtitle(minutes),
            "badge": "",
        }
    )
    tariffs[:] = normalize_and_sort(tariffs)
    print("Тариф добавлен")
    return True


def edit_tariff(tariffs: list[dict]) -> bool:
    if not tariffs:
        print("Тарифов нет")
        return False

    list_tariffs(tariffs)
    idx = ask_int("\nНомер тарифа для изменения: ") - 1
    if idx < 0 or idx >= len(tariffs):
        print("Неверный номер")
        return False

    current = tariffs[idx]
    print(
        f"Редактирование: {current['title']} "
        f"({current['minutes']} мин, {current['amount']} ₸)"
    )

    minutes = ask_int("Новое время доступа (мин): ")
    amount = ask_int("Новая цена (₸): ")

    for i, t in enumerate(tariffs):
        if i != idx and int(t["amount"]) == amount:
            print(f"Нельзя: цена {amount} ₸ уже занята другим тарифом")
            return False

    current["minutes"] = minutes
    current["amount"] = amount
    current["title"] = default_title(minutes)
    current["subtitle"] = default_subtitle(minutes)
    tariffs[:] = normalize_and_sort(tariffs)
    print("Тариф изменен")
    return True


def main() -> None:
    tariffs = load_tariffs()

    while True:
        print("\n=== WiFi-Pay Tariff Manager ===")
        print("1) Показать тарифы")
        print("2) Добавить тариф")
        print("3) Изменить тариф")
        print("4) Сохранить и выйти")
        print("5) Выйти без сохранения")

        choice = input("Выберите действие: ").strip()

        if choice == "1":
            list_tariffs(tariffs)
        elif choice == "2":
            if add_tariff(tariffs):
                save_tariffs(tariffs)
                print(f"Автосохранено: {TARIFFS_FILE}")
                print(f"Автообновлено: {INDEX_TEMPLATE_FILE}")
        elif choice == "3":
            if edit_tariff(tariffs):
                save_tariffs(tariffs)
                print(f"Автосохранено: {TARIFFS_FILE}")
                print(f"Автообновлено: {INDEX_TEMPLATE_FILE}")
        elif choice == "4":
            save_tariffs(tariffs)
            print(f"Сохранено: {TARIFFS_FILE}")
            print(f"Обновлено: {INDEX_TEMPLATE_FILE}")
            print("Перезапустите сервис: sudo systemctl restart wifi-pay")
            return
        elif choice == "5":
            print("Выход без сохранения")
            return
        else:
            print("Неизвестный пункт меню")


if __name__ == "__main__":
    main()
