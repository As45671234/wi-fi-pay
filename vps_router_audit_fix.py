#!/usr/bin/env python3
import argparse
import json
import sys
from typing import Dict, List

import routeros_api


REQUIRED_WG_IP = "10.0.0.1"
REQUIRED_WALLED_GARDEN_HOSTS = [
    "wifi-pay.kz",
    "www.wifi-pay.kz",
    "api.freedompay.kz",
]


def load_routers_config() -> List[Dict]:
    with open("routers_config.json", "r", encoding="utf-8") as f:
        return json.load(f)


def load_router_config(router_id: str) -> Dict:
    routers = load_routers_config()
    for r in routers:
        if r.get("id") == router_id:
            return r
    raise ValueError(f"router_id not found: {router_id}")


def connect_api(cfg: Dict):
    pool = routeros_api.RouterOsApiPool(
        cfg["ip"],
        username=cfg["user"],
        password=cfg["pass"],
        port=int(cfg.get("port", 8728)),
        plaintext_login=True,
    )
    return pool, pool.get_api()


def first(items: List[Dict], pred):
    for x in items:
        if pred(x):
            return x
    return None


def has_all(entry: Dict, expected: Dict) -> bool:
    for k, v in expected.items():
        if str(entry.get(k, "")) != str(v):
            return False
    return True


def ensure_rule(resource, expected: Dict, apply: bool, name: str) -> bool:
    rows = resource.call("print")
    if first(rows, lambda r: has_all(r, expected)):
        print(f"[OK] {name}")
        return False
    print(f"[MISS] {name}")
    if apply:
        resource.call("add", arguments=expected)
        print(f"[FIX] Added: {name}")
        return True
    return False


def ensure_walled_garden(api, apply: bool) -> int:
    wg = api.get_resource("/ip/hotspot/walled-garden")
    rows = wg.call("print")
    changed = 0

    for host in REQUIRED_WALLED_GARDEN_HOSTS:
        exists = first(rows, lambda r: str(r.get("dst-host", "")).lower() == host)
        if exists:
            print(f"[OK] Walled garden host: {host}")
            continue
        print(f"[MISS] Walled garden host: {host}")
        if apply:
            wg.call("add", arguments={"action": "allow", "dst-host": host})
            print(f"[FIX] Added walled garden host: {host}")
            changed += 1
    return changed


def check_api_service(api, apply: bool) -> int:
    svc = api.get_resource("/ip/service")
    rows = svc.call("print")
    api_row = first(rows, lambda r: r.get("name") == "api")
    changed = 0

    if not api_row:
        print("[WARN] '/ip/service name=api' not found")
        return changed

    disabled = str(api_row.get("disabled", "false")).lower() in ("true", "yes")
    port = str(api_row.get("port", ""))

    if not disabled and port == "8728":
        print("[OK] API service enabled on port 8728")
        return changed

    print(f"[MISS] API service state disabled={api_row.get('disabled')} port={port}")
    if apply:
        svc.call(
            "set",
            arguments={
                ".id": api_row.get("id") or api_row.get(".id"),
                "disabled": "no",
                "port": "8728",
                "address": "0.0.0.0/0",
            },
        )
        print("[FIX] API service set to enabled, port 8728, address 0.0.0.0/0")
        changed += 1

    return changed


def audit_and_fix(router_id: str, apply: bool) -> int:
    cfg = load_router_config(router_id)
    pool = None
    changes = 0

    print(f"Router: {cfg['id']} ({cfg['ip']})")
    print(f"Mode: {'APPLY' if apply else 'CHECK-ONLY'}")

    try:
        pool, api = connect_api(cfg)

        changes += check_api_service(api, apply)

        fw_filter = api.get_resource("/ip/firewall/filter")
        fw_nat = api.get_resource("/ip/firewall/nat")
        if_list_member = api.get_resource("/interface/list/member")

        changes += int(
            ensure_rule(
                fw_filter,
                {
                    "chain": "input",
                    "action": "accept",
                    "protocol": "tcp",
                    "src-address": REQUIRED_WG_IP,
                    "dst-port": "8728",
                    "in-interface": "wireguard1",
                    "comment": "allow_api_from_vps",
                },
                apply,
                "Firewall input allow API from VPS",
            )
        )

        changes += int(
            ensure_rule(
                fw_filter,
                {
                    "chain": "forward",
                    "action": "accept",
                    "in-interface": "bridge",
                    "out-interface": "ether1",
                    "comment": "FORWARD_LAN_TO_WAN",
                },
                apply,
                "Forward LAN->WAN",
            )
        )

        changes += int(
            ensure_rule(
                fw_filter,
                {
                    "chain": "forward",
                    "action": "accept",
                    "in-interface": "ether1",
                    "out-interface": "bridge",
                    "comment": "FORWARD_WAN_TO_LAN",
                },
                apply,
                "Forward WAN->LAN",
            )
        )

        changes += int(
            ensure_rule(
                fw_nat,
                {
                    "chain": "srcnat",
                    "action": "masquerade",
                    "out-interface": "ether1",
                    "comment": "NAT_TO_INTERNET",
                },
                apply,
                "NAT masquerade to internet",
            )
        )

        changes += int(
            ensure_rule(
                if_list_member,
                {"list": "LAN", "interface": "wireguard1"},
                apply,
                "wireguard1 in LAN list",
            )
        )

        changes += ensure_walled_garden(api, apply)

        # Lightweight connectivity probe from router itself.
        print("[CHECK] Router fetch freedompay...")
        try:
            fetch = api.get_resource("/tool/fetch")
            fetch.call(
                "add",
                arguments={
                    "url": "https://api.freedompay.kz/payment.php",
                    "keep-result": "no",
                },
            )
            print("[OK] Router can fetch FreedomPay URL")
        except Exception as e:
            print(f"[WARN] FreedomPay fetch failed: {str(e)[:200]}")

    finally:
        if pool:
            try:
                pool.disconnect()
            except Exception:
                pass

    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit/fix MikroTik from VPS via RouterOS API")
    parser.add_argument(
        "--router-id",
        help="Router id from routers_config.json (e.g. astana_02) or comma-separated list (e.g. astana_02,astana_03)",
    )
    parser.add_argument("--all", action="store_true", help="Run for all routers from routers_config.json")
    parser.add_argument("--apply", action="store_true", help="Apply missing rules (default is check-only)")
    args = parser.parse_args()

    if not args.all and not args.router_id:
        parser.error("Specify --router-id or --all")

    if args.all and args.router_id:
        parser.error("Use either --router-id or --all, not both")

    if args.all:
        target_ids = [r.get("id") for r in load_routers_config() if r.get("id")]
    else:
        target_ids = [x.strip() for x in args.router_id.split(",") if x.strip()]
        if not target_ids:
            parser.error("--router-id cannot be empty")

    total_changes = 0
    failed = []
    for rid in target_ids:
        print("=" * 60)
        try:
            total_changes += audit_and_fix(rid, args.apply)
        except Exception as e:
            failed.append((rid, str(e)))
            print(f"ERROR [{rid}]: {e}")

    print("-" * 60)
    print(f"Targets: {len(target_ids)}")
    print(f"Changes applied: {total_changes}")
    print(f"Failed: {len(failed)}")
    if failed:
        for rid, err in failed:
            print(f"  - {rid}: {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
