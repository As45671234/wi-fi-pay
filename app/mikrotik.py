"""
app/mikrotik.py — All MikroTik RouterOS API interactions.
"""

import socket
import time
from datetime import datetime, timedelta

import inspect as _inspect

import routeros_api

from .config import (
    ROUTERS_CONFIG, PREPARE_TIMEOUT_SECONDS, KZ_TZ,
    QR_FALLBACK_MAX_IDLE_SECONDS, logger,
)
from .utils import _normalize_mac, _is_valid_mac, _routeros_duration_to_seconds

# Check once whether routeros_api supports socket_timeout
_POOL_HAS_SOCKET_TIMEOUT = 'socket_timeout' in _inspect.signature(
    routeros_api.RouterOsApiPool.__init__
).parameters


def _make_router_pool(
    ip: str, username: str, password: str,
    port: int = 8728, socket_timeout: float = 5.0,
) -> routeros_api.RouterOsApiPool:
    """Create RouterOsApiPool, omitting socket_timeout if not supported."""
    kwargs = dict(
        username=username, password=password,
        port=port, plaintext_login=True,
    )
    if _POOL_HAS_SOCKET_TIMEOUT:
        kwargs['socket_timeout'] = socket_timeout
    return routeros_api.RouterOsApiPool(ip, **kwargs)


# ── Connectivity precheck ──────────────────────────────────────────────────

def _router_api_reachable(ip: str, port: int, timeout_sec: float = 0.8) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout_sec):
            return True
    except OSError:
        return False


# ── QR MAC fallback ────────────────────────────────────────────────────────

def _pick_qr_mac_fallback(router_id: str, busy_macs: set[str]) -> tuple[str | None, str]:
    """Безопасный fallback: выбирает MAC только если есть ровно один свежий кандидат."""
    config = ROUTERS_CONFIG.get(router_id)
    if not config:
        return None, "router_unknown"

    connection = None
    try:
        connection = _make_router_pool(
            config['ip'], config['user'], config['pass'],
            port=int(config.get('port', 8728)), socket_timeout=1.0,
        )
        api = connection.get_api()
        host_res = api.get_resource('/ip/hotspot/host')
        rows = host_res.call('print')
    except Exception:
        return None, "router_unavailable"
    finally:
        try:
            if connection:
                connection.disconnect()
        except Exception:
            pass

    if not rows:
        return None, "no_clients"

    candidates = []
    for row in rows:
        mac = _normalize_mac(str(row.get('mac-address') or ""))
        if not _is_valid_mac(mac):
            continue
        if mac in busy_macs:
            continue

        authorized_raw = str(row.get('authorized') or "").strip().lower()
        if authorized_raw in ("true", "yes"):
            continue

        idle_seconds = _routeros_duration_to_seconds(str(row.get('idle-time') or row.get('idle_time') or ""))
        if idle_seconds is None:
            continue
        if idle_seconds > QR_FALLBACK_MAX_IDLE_SECONDS:
            continue

        uptime_seconds = _routeros_duration_to_seconds(str(row.get('uptime') or "")) or 10**9
        candidates.append({"mac": mac, "idle": idle_seconds, "uptime": uptime_seconds})

    if not candidates:
        return None, "no_fresh_candidates"

    candidates.sort(key=lambda c: (c['idle'], c['uptime']))
    if len(candidates) == 1:
        return candidates[0]['mac'], "single_fresh"

    if (candidates[1]['idle'] - candidates[0]['idle']) >= 8:
        return candidates[0]['mac'], "fresh_with_gap"

    return None, "ambiguous"


# ── Access verification ────────────────────────────────────────────────────

def verify_access_activated(api, mac: str, user_name: str, mode: str) -> dict:
    result = {"binding_exists": False, "user_exists": False, "mode": mode}
    try:
        binding = api.get_resource('/ip/hotspot/ip-binding')
        bindings = binding.call('print', queries={'mac-address': mac})
        if bindings:
            for b in bindings:
                comment = b.get('comment', '')
                if mode in comment:
                    result["binding_exists"] = True
                    logger.debug(f"  ✓ Биндинг найден: {comment}")
                    break
    except Exception as e:
        logger.warning(f"  Ошибка проверки биндинга: {str(e)[:100]}")

    try:
        user_res = api.get_resource('/ip/hotspot/user')
        users = user_res.call('print', queries={'name': user_name})
        if users:
            for u in users:
                comment = u.get('comment', '')
                if mode in comment:
                    result["user_exists"] = True
                    logger.debug(f"  ✓ Юзер найден: {comment}")
                    break
    except Exception as e:
        logger.warning(f"  Ошибка проверки юзера: {str(e)[:100]}")

    return result


def check_router_hotspot_enabled(config: dict) -> bool:
    try:
        connection = _make_router_pool(
            config['ip'], config['user'], config['pass'],
            port=config.get('port', 8728), socket_timeout=5.0,
        )
        api = connection.get_api()
        hotspot_profiles = api.get_resource('/ip/hotspot/profile').call('print')
        if not hotspot_profiles:
            logger.error(f"  ❌ На роутере {config['ip']} нет профилей hotspot!")
            connection.disconnect()
            return False
        logger.info(f"  ✓ Hotspot профили найдены на {config['ip']}")
        connection.disconnect()
        return True
    except Exception as e:
        logger.error(f"  ❌ Не удалось проверить hotspot на {config['ip']}: {str(e)[:150]}")
        return False


# ── Internal MikroTik helpers ──────────────────────────────────────────────

def _mikrotik_check_existing_access(binding, user_res, user_name, mac):
    for b in binding.call('print', queries={'mac-address': mac}):
        comment = (b.get('comment') or '')
        if comment.startswith('TRIAL_') or comment.startswith('PAID_'):
            logger.info(f"Защита: {comment} уже активен для {mac[:8]}***, не перетирать")
            return True
    for u in user_res.call('print', queries={'name': user_name}):
        comment = (u.get('comment') or '')
        if comment.startswith('TRIAL_') or comment.startswith('PAID_'):
            logger.info(f"Защита: {comment} юзер уже активен для {mac[:8]}***, не перетирать")
            return True
    return False


def _mikrotik_cleanup_old(binding, active, user_res, mac, user_name, mode):
    for b in binding.call('print', queries={'mac-address': mac}):
        comment = (b.get('comment') or '')
        if mode == 'PAY_WINDOW' and (comment.startswith('PAID_') or comment.startswith('TRIAL_')):
            logger.info(f"PAY_WINDOW: защита {comment} - не удаляем")
            continue
        try:
            binding.call('remove', arguments={'.id': b.get('id') or b.get('.id')})
            logger.debug(f"Удален биндинг {mac}: {comment}")
        except Exception as e:
            logger.warning(f"Ошибка при удалении биндинга {mac}: {str(e)[:100]}")

    for a in active.call('print', queries={'mac-address': mac}):
        try:
            active.call('remove', arguments={'.id': a.get('id') or a.get('.id')})
        except Exception as e:
            logger.warning(f"Ошибка при удалении active {mac}: {str(e)[:100]}")

    for u in user_res.call('print', queries={'name': user_name}):
        try:
            user_res.call('remove', arguments={'.id': u.get('id') or u.get('.id')})
        except Exception as e:
            logger.warning(f"Ошибка при удалении юзера {user_name}: {str(e)[:100]}")


def _mikrotik_create_access(binding, active, user_res, host_res, mac, user_name, user_pass, minutes, mode):
    if mode in ('PAY_WINDOW', 'TRIAL'):
        try:
            binding.call('add', arguments={'mac-address': mac, 'type': 'bypassed', 'comment': f"{mode}_{mac}"})
            logger.info(f"✓ Биндинг {mode} добавлен для {mac[:8]}***")
        except Exception as e:
            logger.error(f"❌ ОШИБКА добавления биндинга {mode} для {mac}: {str(e)[:150]}")
            raise
    else:
        user_res.call('add', arguments={
            'name': user_name,
            'password': user_pass,
            'limit-uptime': f"{minutes}m",
            'comment': f"{mode}_{mac}",
        })

        host_ip = None
        access_mode = "BYPASS"

        for _ in range(5):
            hosts = host_res.call('print', queries={'mac-address': mac})
            if hosts:
                host_ip = hosts[0].get('address')
            if host_ip:
                login_args = {'user': user_name, 'password': user_pass, 'mac-address': mac, 'ip': host_ip}
                try:
                    active.call('login', arguments=login_args)
                    time.sleep(0.25)
                    active_rows = active.call('print', queries={'mac-address': mac})
                    if active_rows:
                        access_mode = "ACTIVE"
                        break
                except Exception:
                    pass
            time.sleep(0.35)

        paid_binding_exists = False
        for b in binding.call('print', queries={'mac-address': mac}):
            comment = (b.get('comment') or '')
            if comment.startswith(f"{mode}_"):
                paid_binding_exists = True
                break

        if not paid_binding_exists:
            try:
                binding.call('add', arguments={'mac-address': mac, 'type': 'bypassed', 'comment': f"{mode}_{mac}"})
                if access_mode == "ACTIVE":
                    logger.info(f"PAID страховка: добавлен BYPASS биндинг для {mac[:8]}***")
                else:
                    logger.info(f"Fallback: биндинг BYPASS добавлен для {mac[:8]}***")
            except Exception as e:
                logger.error(f"❌ Ошибка добавления BYPASS-биндинга для {mac}: {str(e)[:150]}")


def _mikrotik_setup_scheduler(api, sched, mac, user_name, mode, seconds, minutes):
    try:
        clock_info = api.get_resource('/system/clock').call('print')[0]
        date_str = clock_info.get('date', '')
        time_str = clock_info.get('time', '')
        mt_now = None
        for fmt in ("%b/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                mt_now = datetime.strptime(f"{date_str} {time_str}", fmt)
                break
            except ValueError:
                try:
                    mt_now = datetime.strptime(f"{date_str.title()} {time_str}", fmt)
                    break
                except ValueError:
                    continue
        if mt_now is None:
            raise ValueError(f"Unknown clock format: '{date_str}' '{time_str}'")
    except Exception as e:
        logger.error(f"Clock parse error: {e}")
        mt_now = datetime.now(KZ_TZ).replace(tzinfo=None)

    duration_seconds = max(1, int(seconds if seconds is not None else round(minutes * 60)))
    mt_expiry = mt_now + timedelta(seconds=duration_seconds)
    mt_date = mt_expiry.strftime("%b/%d/%Y").lower()
    mt_time = mt_expiry.strftime("%H:%M:%S")
    task_name = f"del_{mac.replace(':', '')}"

    on_event = (
        f':do {{ /ip hotspot active remove [find mac-address="{mac}"] }} on-error={{}}; '
        f':do {{ /ip hotspot cookie remove [find mac-address="{mac}"] }} on-error={{}}; '
        f':do {{ /ip hotspot host remove [find mac-address="{mac}"] }} on-error={{}}; '
        f':do {{ /ip hotspot ip-binding remove [find mac-address="{mac}"] }} on-error={{}}; '
        f':do {{ /ip hotspot user remove [find name="{user_name}"] }} on-error={{}}; '
        f':do {{ /system scheduler remove [find name="{task_name}"] }} on-error={{}}; '
    )

    for t in sched.call('print', queries={'name': task_name}):
        try:
            sched.call('remove', arguments={'.id': t.get('id') or t.get('.id')})
            logger.debug(f"Старый scheduler {task_name} удален")
        except Exception as e:
            logger.warning(f"Ошибка удаления старого scheduler {task_name}: {str(e)[:100]}")

    try:
        sched.call('add', arguments={
            'name': task_name,
            'start-date': mt_date,
            'start-time': mt_time,
            'interval': "00:00:00",
            'on-event': on_event,
            'comment': f"AUTOCLEAR_{mode}_{mac}",
        })
        logger.info(f"✓ Scheduler {task_name} установлен на {mt_date} {mt_time} для очистки {mode} через {duration_seconds}с")
    except Exception as e:
        logger.error(f"❌ КРИТИЧЕСКАЯ ОШИБКА при создании scheduler {task_name}: {str(e)[:150]}")
        raise


# ── Core: set_mikrotik_ah_access ──────────────────────────────────────────

def set_mikrotik_ah_access(mac: str, router_id: str, minutes: int, mode: str, seconds: int | None = None):
    config = ROUTERS_CONFIG.get(router_id)
    if not config:
        logger.error(f"❌ Неизвестный router_id: {router_id}")
        return False

    if not _is_valid_mac(mac or ""):
        logger.error(f"❌ Некорректный MAC: {mac}")
        return False

    connection = None
    max_retries = 1 if mode == 'PAY_WINDOW' else 3
    api_port = int(config.get('port', 8728))

    if mode == 'PAY_WINDOW' and not _router_api_reachable(config['ip'], api_port):
        logger.error(f"[MK] API недоступен {router_id} ({config['ip']}:{api_port})")
        return False

    for attempt in range(1, max_retries + 1):
        try:
            t0 = time.monotonic()
            connection = _make_router_pool(
                config['ip'], config['user'], config['pass'],
                port=api_port, socket_timeout=8.0,
            )
            api = connection.get_api()
            logger.info(f"[MK] connect {router_id}: {(time.monotonic()-t0)*1000:.0f}ms (attempt {attempt})")

            if mode == 'PAY_WINDOW':
                t1 = time.monotonic()
                binding = api.get_resource('/ip/hotspot/ip-binding')
                user_res = api.get_resource('/ip/hotspot/user')
                active = api.get_resource('/ip/hotspot/active')
                sched = api.get_resource('/system/scheduler')
                user_name = f"T-{mac.replace(':', '')}"

                for b in binding.call('print', queries={'mac-address': mac}):
                    comment = (b.get('comment') or '')
                    if comment.startswith('PAID_') or comment.startswith('TRIAL_'):
                        logger.info(f"PAY_WINDOW: уже есть {comment} для {mac[:8]}***, пропускаем")
                        return True
                    try:
                        binding.call('remove', arguments={'.id': b.get('id') or b.get('.id')})
                    except Exception:
                        pass

                for u in user_res.call('print', queries={'name': user_name}):
                    comment = (u.get('comment') or '')
                    if comment.startswith('PAID_') or comment.startswith('TRIAL_'):
                        has_active = False
                        has_protected_binding = False
                        try:
                            has_active = bool(active.call('print', queries={'mac-address': mac}))
                        except Exception:
                            has_active = False

                        if not has_active:
                            try:
                                for pb in binding.call('print', queries={'mac-address': mac}):
                                    pcomment = (pb.get('comment') or '')
                                    if pcomment.startswith('PAID_') or pcomment.startswith('TRIAL_'):
                                        has_protected_binding = True
                                        break
                            except Exception:
                                has_protected_binding = False

                        if has_active or has_protected_binding:
                            logger.info(f"PAY_WINDOW: уже есть user {comment} для {mac[:8]}***, пропускаем")
                            return True

                        logger.info(f"PAY_WINDOW: найден stale user {comment} для {mac[:8]}***, продолжаем создание PAY_WINDOW")

                logger.info(f"[MK] binding print+cleanup: {(time.monotonic()-t1)*1000:.0f}ms")
                t2 = time.monotonic()
                binding.call('add', arguments={'mac-address': mac, 'type': 'bypassed', 'comment': f"PAY_WINDOW_{mac}"})
                logger.info(f"[MK] binding add: {(time.monotonic()-t2)*1000:.0f}ms")
                t3 = time.monotonic()
                _mikrotik_setup_scheduler(api, sched, mac, user_name, "PAY_WINDOW", seconds, minutes)
                logger.info(f"[MK] scheduler: {(time.monotonic()-t3)*1000:.0f}ms, PAY_WINDOW total: {(time.monotonic()-t0)*1000:.0f}ms")
                return True

            binding = api.get_resource('/ip/hotspot/ip-binding')
            active = api.get_resource('/ip/hotspot/active')
            user_res = api.get_resource('/ip/hotspot/user')
            host_res = api.get_resource('/ip/hotspot/host')
            sched = api.get_resource('/system/scheduler')

            user_name = f"T-{mac.replace(':', '')}"
            user_pass = f"p{int(time.time()) % 1000000}"

            _mikrotik_cleanup_old(binding, active, user_res, mac, user_name, mode)
            _mikrotik_create_access(binding, active, user_res, host_res, mac, user_name, user_pass, minutes, mode)
            _mikrotik_setup_scheduler(api, sched, mac, user_name, mode, seconds, minutes)

            if mode == 'PAID':
                logger.info(f"[VERIFY] Проверяю что {mode} реально активирован...")
                verify_result = verify_access_activated(api, mac, user_name, mode)
                if verify_result["binding_exists"] or verify_result["user_exists"]:
                    logger.info(f"✅ ПОДТВЕРЖЕНО: {mode} активирован для {mac[:8]}*** ({'биндинг' if verify_result['binding_exists'] else 'юзер'})")
                else:
                    logger.error(f"⚠️ ВНИМАНИЕ: {mode} МОЖЕТ НЕ АКТИВИРОВАН для {mac[:8]}*** (проверить на роутере вручную!)")

            if mode in ['PAID', 'PAY_WINDOW']:
                logger.info(f"Access granted: {mode} {minutes}min for {mac[:8]}***")
            return True
        except Exception as e:
            logger.error(f"MikroTik API error for {router_id} (attempt {attempt}/{max_retries}): {str(e)[:200]}")
            if connection:
                try:
                    connection.disconnect()
                except Exception:
                    pass
            if attempt == max_retries:
                logger.error(f"❌ Не удалось подключиться к роутеру {router_id} после {max_retries} попыток.")
                return False
            time.sleep(1)
        finally:
            if connection:
                try:
                    connection.disconnect()
                except Exception:
                    pass
