#!/usr/bin/env python3
"""Меняет пароль страницы /driver_access (выдача доступа водителям автобусов).

Использование:
  python manage_driver_password.py <новый_пароль>
  python manage_driver_password.py            # спросит пароль интерактивно

Пишет DRIVER_ACCESS_PASSWORD в .env. Перезапустите сервис после смены:
  sudo systemctl restart wifi-pay
"""

from __future__ import annotations

import sys
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent / ".env"
VAR_NAME = "DRIVER_ACCESS_PASSWORD"


def set_env_var(name: str, value: str) -> None:
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    prefix = f"{name}="
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[i] = f"{name}={value}"
            found = True
            break
    if not found:
        lines.append(f"{name}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    if len(sys.argv) > 1:
        new_password = sys.argv[1].strip()
    else:
        new_password = input("Новый пароль для /driver_access: ").strip()

    if not new_password:
        print("Пароль не может быть пустым")
        sys.exit(1)
    if len(new_password) < 6:
        print("Пароль слишком короткий (минимум 6 символов)")
        sys.exit(1)

    set_env_var(VAR_NAME, new_password)
    print(f"Пароль обновлён в {ENV_FILE}")
    print("Перезапустите сервис: sudo systemctl restart wifi-pay")


if __name__ == "__main__":
    main()
