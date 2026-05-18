#!/bin/bash
# Показывает всех текущих клиентов и остаток времени для active/bypassed сессий

if [ -f ".env" ]; then
    set -a
    . ./.env
    set +a
fi

python3 << 'PYTHON_SCRIPT'
import json
import os
from datetime import datetime

import routeros_api

router_ips_raw = os.getenv('ROUTER_IPS', os.getenv('ROUTER_IP', '10.0.0.2'))

if router_ips_raw.strip().lower() == '-all':
    with open('routers_config.json', encoding='utf-8-sig') as f:
        routers = json.load(f)
    ROUTER_IPS = [router.get('ip', '').strip() for router in routers if router.get('ip')]
else:
    ROUTER_IPS = [ip.strip() for ip in router_ips_raw.split(',') if ip.strip()]

ROUTER_USER = os.getenv('ROUTER_USER', 'admin')
ROUTER_PASS = os.getenv('ROUTER_PASS', 'kaspiwifiadmin2026')


def parse_scheduler_dt(date_str, time_str):
    try:
        return datetime.strptime(f"{date_str} {time_str}", "%b/%d/%Y %H:%M:%S")
    except Exception:
        return None


def format_remaining(target_dt):
    if not target_dt:
        return "N/A"
    remaining = int((target_dt - datetime.now()).total_seconds())
    if remaining <= 0:
        return "expired"
    hours, rem = divmod(remaining, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def scheduler_map(api):
    sched = api.get_resource('/system/scheduler')
    result = {}
    for task in sched.get():
        name = task.get('name', '')
        if not name.startswith('del_'):
            continue
        mac_hex = name[4:].upper()
        result[mac_hex] = {
            'start-date': task.get('start-date', ''),
            'start-time': task.get('start-time', ''),
            'comment': task.get('comment', ''),
        }
    return result


def mac_to_hex(mac):
    return (mac or '').replace(':', '').upper()


print('══════════════════════════════════════════════════════════════════════')
print('ТЕКУЩИЕ КЛИЕНТЫ WIFI-PAY')
print('══════════════════════════════════════════════════════════════════════')

reachable = 0

for router_ip in ROUTER_IPS:
    print(f"\nROUTER: {router_ip}")
    print('----------------------------------------------------------------------')
    connection = None
    try:
        connection = routeros_api.RouterOsApiPool(
            router_ip,
            username=ROUTER_USER,
            password=ROUTER_PASS,
            plaintext_login=True,
        )
        api = connection.get_api()
        reachable += 1

        active = api.get_resource('/ip/hotspot/active').get()
        bindings = api.get_resource('/ip/hotspot/ip-binding').get()
        sched_by_mac = scheduler_map(api)

        shown = set()

        if active:
            print('ACTIVE:')
            print('----------------------------------------------------------------------')
            for row in active:
                mac = row.get('mac-address', '')
                shown.add(mac)
                print(f"MAC: {mac}")
                print(f"  Mode: ACTIVE")
                print(f"  User: {row.get('user', 'N/A')}")
                print(f"  IP: {row.get('address', 'N/A')}")
                print(f"  Uptime: {row.get('uptime', 'N/A')}")
                print(f"  Session left: {row.get('session-time-left', 'N/A')}")

        interesting_bindings = []
        for row in bindings:
            comment = row.get('comment', '') or ''
            if not (comment.startswith('PAY_WINDOW_') or comment.startswith('PAID_') or comment.startswith('TRIAL_')):
                continue
            mac = row.get('mac-address', '')
            if mac in shown:
                continue
            interesting_bindings.append(row)

        if interesting_bindings:
            print('BYPASSED / IP-BINDINGS:')
            print('----------------------------------------------------------------------')
            for row in interesting_bindings:
                mac = row.get('mac-address', '')
                sched = sched_by_mac.get(mac_to_hex(mac), {})
                target_dt = parse_scheduler_dt(sched.get('start-date', ''), sched.get('start-time', ''))
                print(f"MAC: {mac}")
                print(f"  Mode: {row.get('comment', 'N/A')}")
                print(f"  Binding type: {row.get('type', 'N/A')}")
                print(f"  Remaining: {format_remaining(target_dt)}")
                print(f"  Ends at: {sched.get('start-date', 'N/A')} {sched.get('start-time', 'N/A')}")

        if not active and not interesting_bindings:
            print('Нет текущих клиентов.')

    except Exception as e:
        print(f"Ошибка подключения к {router_ip}: {e}")
    finally:
        if connection:
            connection.disconnect()

if reachable == 0:
    print('\nНе удалось подключиться ни к одному роутеру.')

print('\n══════════════════════════════════════════════════════════════════════')
PYTHON_SCRIPT