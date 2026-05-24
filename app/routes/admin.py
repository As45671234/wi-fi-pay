"""
app/routes/admin.py — Admin/diagnostic routes: /health, /debug, /api/admin/stats,
                      /admin/stats, /api/activation/process_pending.
"""

import hmac as _hmac
import asyncio
import sqlite3
import time
from datetime import datetime, timedelta

import routeros_api
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..config import ADMIN_TOKEN, ROUTERS_CONFIG, KZ_TZ, templates, logger
from ..db import get_db
from ..utils import utf8_json_response, load_tariffs_config
from ..mikrotik import check_router_hotspot_enabled
from ..pending import _drain_pending_activations
from ..utils import _is_valid_mac

router = APIRouter()

_stats_cache: "dict | None" = None
_stats_cache_ts: float = 0.0
_STATS_CACHE_TTL = 60.0


def _has_admin_auth(request: Request) -> bool:
    """Check ADMIN_TOKEN for internal diagnostic endpoints. If token not set, disables access."""
    if not ADMIN_TOKEN:
        return False
    header = (request.headers.get("x-admin-token") or "").strip()
    auth = (request.headers.get("authorization") or "").strip()
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    return _hmac.compare_digest(header, ADMIN_TOKEN) or _hmac.compare_digest(bearer, ADMIN_TOKEN)


def _collect_router_stats() -> dict:
    """
    Агрегирует покупки из обеих таблиц (FreedomPay + Kaspi) по роутерам.
    Результат кешируется на 60 секунд.
    """
    global _stats_cache, _stats_cache_ts
    _mono = time.monotonic()
    if _stats_cache is not None and (_mono - _stats_cache_ts) < _STATS_CACHE_TTL:
        return _stats_cache

    now_kz = datetime.now(KZ_TZ)
    today_str = now_kz.strftime("%Y-%m-%d")
    week_ago_utc = (datetime.utcnow() - timedelta(days=7)).isoformat()
    month_ago_utc = (datetime.utcnow() - timedelta(days=30)).isoformat()

    conn = get_db()
    conn.row_factory = sqlite3.Row
    try:
        fp_rows = conn.execute("""
            SELECT
                router_id,
                amount,
                COUNT(*) AS cnt,
                SUM(amount) AS revenue,
                SUM(CASE WHEN date(created_at, '+5 hours') = ? THEN 1 ELSE 0 END) AS today_cnt,
                SUM(CASE WHEN date(created_at, '+5 hours') = ? THEN amount ELSE 0 END) AS today_revenue,
                SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS week_cnt,
                SUM(CASE WHEN created_at >= ? THEN amount ELSE 0 END) AS week_revenue,
                SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS month_cnt,
                SUM(CASE WHEN created_at >= ? THEN amount ELSE 0 END) AS month_revenue
            FROM orders
            WHERE status = 'PAID' AND router_id IS NOT NULL AND amount > 0
            GROUP BY router_id, amount
            ORDER BY router_id, amount
        """, (today_str, today_str, week_ago_utc, week_ago_utc, month_ago_utc, month_ago_utc)).fetchall()

        kaspi_rows = conn.execute("""
            SELECT
                router_id,
                amount,
                COUNT(*) AS cnt,
                SUM(amount) AS revenue,
                SUM(CASE WHEN date(COALESCE(activated_at, created_at), '+5 hours') = ? THEN 1 ELSE 0 END) AS today_cnt,
                SUM(CASE WHEN date(COALESCE(activated_at, created_at), '+5 hours') = ? THEN amount ELSE 0 END) AS today_revenue,
                SUM(CASE WHEN COALESCE(activated_at, created_at) >= ? THEN 1 ELSE 0 END) AS week_cnt,
                SUM(CASE WHEN COALESCE(activated_at, created_at) >= ? THEN amount ELSE 0 END) AS week_revenue,
                SUM(CASE WHEN COALESCE(activated_at, created_at) >= ? THEN 1 ELSE 0 END) AS month_cnt,
                SUM(CASE WHEN COALESCE(activated_at, created_at) >= ? THEN amount ELSE 0 END) AS month_revenue
            FROM kaspi_orders
            WHERE is_activated = 1 AND router_id IS NOT NULL AND amount > 0
            GROUP BY router_id, amount
            ORDER BY router_id, amount
        """, (today_str, today_str, week_ago_utc, week_ago_utc, month_ago_utc, month_ago_utc)).fetchall()

        trial_rows = conn.execute("""
            SELECT
                router_id,
                COUNT(*) AS cnt,
                SUM(CASE WHEN date(created_at, '+5 hours') = ? THEN 1 ELSE 0 END) AS today_cnt,
                SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS week_cnt,
                SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS month_cnt
            FROM orders
            WHERE status = 'TRIAL' AND router_id IS NOT NULL
            GROUP BY router_id
            ORDER BY router_id
        """, (today_str, week_ago_utc, month_ago_utc)).fetchall()
    finally:
        conn.close()

    routers: dict = {}

    def _ensure_router(rid: str) -> dict:
        if rid not in routers:
            routers[rid] = {
                "freedompay": {"total": 0, "revenue": 0, "today": 0, "today_revenue": 0, "week": 0, "week_revenue": 0, "month": 0, "month_revenue": 0, "by_tariff": {}},
                "kaspi":      {"total": 0, "revenue": 0, "today": 0, "today_revenue": 0, "week": 0, "week_revenue": 0, "month": 0, "month_revenue": 0, "by_tariff": {}},
                "trial":      {"total": 0, "today": 0, "week": 0, "month": 0},
            }
        return routers[rid]

    for r in fp_rows:
        d = _ensure_router(r["router_id"])["freedompay"]
        d["total"]   += r["cnt"]
        d["revenue"] += r["revenue"] or 0
        d["today"]   += r["today_cnt"] or 0
        d["today_revenue"] += r["today_revenue"] or 0
        d["week"]    += r["week_cnt"] or 0
        d["week_revenue"] += r["week_revenue"] or 0
        d["month"]   += r["month_cnt"] or 0
        d["month_revenue"] += r["month_revenue"] or 0
        d["by_tariff"][str(r["amount"])] = {
            "count":   r["cnt"],
            "revenue": r["revenue"],
            "today":   r["today_cnt"],
            "today_revenue": r["today_revenue"],
            "week":    r["week_cnt"],
            "week_revenue": r["week_revenue"],
            "month":   r["month_cnt"],
            "month_revenue": r["month_revenue"],
        }

    for r in kaspi_rows:
        d = _ensure_router(r["router_id"])["kaspi"]
        d["total"]   += r["cnt"]
        d["revenue"] += r["revenue"] or 0
        d["today"]   += r["today_cnt"] or 0
        d["today_revenue"] += r["today_revenue"] or 0
        d["week"]    += r["week_cnt"] or 0
        d["week_revenue"] += r["week_revenue"] or 0
        d["month"]   += r["month_cnt"] or 0
        d["month_revenue"] += r["month_revenue"] or 0
        d["by_tariff"][str(r["amount"])] = {
            "count":   r["cnt"],
            "revenue": r["revenue"],
            "today":   r["today_cnt"],
            "today_revenue": r["today_revenue"],
            "week":    r["week_cnt"],
            "week_revenue": r["week_revenue"],
            "month":   r["month_cnt"],
            "month_revenue": r["month_revenue"],
        }

    for r in trial_rows:
        d = _ensure_router(r["router_id"])["trial"]
        d["total"] += r["cnt"]
        d["today"] += r["today_cnt"]
        d["week"]  += r["week_cnt"]
        d["month"] += r["month_cnt"]

    for rid, rd in routers.items():
        fp = rd["freedompay"]
        ka = rd["kaspi"]
        tr = rd["trial"]
        rd["summary"] = {
            "total_paid":    fp["total"] + ka["total"],
            "total_revenue": fp["revenue"] + ka["revenue"],
            "total_trial":   tr["total"],
            "today_paid":    fp["today"] + ka["today"],
            "today_revenue": fp["today_revenue"] + ka["today_revenue"],
            "today_trial":   tr["today"],
            "week_paid":     fp["week"] + ka["week"],
            "week_revenue":  fp["week_revenue"] + ka["week_revenue"],
            "month_paid":    fp["month"] + ka["month"],
            "month_revenue": fp["month_revenue"] + ka["month_revenue"],
        }

    all_fp_total    = sum(rd["freedompay"]["total"]   for rd in routers.values())
    all_fp_revenue  = sum(rd["freedompay"]["revenue"] for rd in routers.values())
    all_ka_total    = sum(rd["kaspi"]["total"]        for rd in routers.values())
    all_ka_revenue  = sum(rd["kaspi"]["revenue"]      for rd in routers.values())
    all_trial_total = sum(rd["trial"]["total"]        for rd in routers.values())
    all_today       = sum(rd["summary"]["today_paid"] for rd in routers.values())
    all_week        = sum(rd["summary"]["week_paid"]  for rd in routers.values())
    all_month       = sum(rd["summary"]["month_paid"] for rd in routers.values())

    routers = dict(sorted(routers.items()))

    _result = {
        "generated_at": now_kz.isoformat(),
        "today_date":   today_str,
        "routers":      routers,
        "totals": {
            "total_paid":       all_fp_total + all_ka_total,
            "total_revenue":    all_fp_revenue + all_ka_revenue,
            "freedompay_paid":  all_fp_total,
            "freedompay_revenue": all_fp_revenue,
            "kaspi_paid":       all_ka_total,
            "kaspi_revenue":    all_ka_revenue,
            "trial_total":      all_trial_total,
            "today_paid":       all_today,
            "today_revenue":    sum(rd["summary"]["today_revenue"] for rd in routers.values()),
            "week_paid":        all_week,
            "week_revenue":     sum(rd["summary"]["week_revenue"] for rd in routers.values()),
            "month_paid":       all_month,
            "month_revenue":    sum(rd["summary"]["month_revenue"] for rd in routers.values()),
        },
    }
    _stats_cache = _result
    _stats_cache_ts = time.monotonic()
    return _result


def _collect_router_stats_range(from_date: str, to_date: str) -> dict:
    """Aggregate stats for a custom date range (KZ time UTC+5, both dates inclusive)."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    try:
        fp_rows = conn.execute("""
            SELECT router_id, amount, COUNT(*) AS cnt, SUM(amount) AS revenue
            FROM orders
            WHERE status = 'PAID' AND router_id IS NOT NULL AND amount > 0
              AND date(created_at, '+5 hours') BETWEEN ? AND ?
            GROUP BY router_id, amount
        """, (from_date, to_date)).fetchall()

        kaspi_rows = conn.execute("""
            SELECT router_id, amount, COUNT(*) AS cnt, SUM(amount) AS revenue
            FROM kaspi_orders
            WHERE is_activated = 1 AND router_id IS NOT NULL AND amount > 0
              AND date(COALESCE(activated_at, created_at), '+5 hours') BETWEEN ? AND ?
            GROUP BY router_id, amount
        """, (from_date, to_date)).fetchall()

        trial_rows = conn.execute("""
            SELECT router_id, COUNT(*) AS cnt
            FROM orders
            WHERE status = 'TRIAL' AND router_id IS NOT NULL
              AND date(created_at, '+5 hours') BETWEEN ? AND ?
            GROUP BY router_id
        """, (from_date, to_date)).fetchall()
    finally:
        conn.close()

    routers: dict = {}

    def _ensure(rid: str) -> dict:
        if rid not in routers:
            routers[rid] = {"paid": 0, "revenue": 0, "fp": 0, "fp_revenue": 0,
                            "ka": 0, "ka_revenue": 0, "trial": 0, "by_tariff": {}}
        return routers[rid]

    for r in fp_rows:
        d = _ensure(r["router_id"])
        cnt, rev = r["cnt"], r["revenue"] or 0
        d["paid"] += cnt; d["revenue"] += rev; d["fp"] += cnt; d["fp_revenue"] += rev
        bt = d["by_tariff"].setdefault(str(r["amount"]), {"count": 0, "fp": 0, "ka": 0})
        bt["count"] += cnt; bt["fp"] += cnt

    for r in kaspi_rows:
        d = _ensure(r["router_id"])
        cnt, rev = r["cnt"], r["revenue"] or 0
        d["paid"] += cnt; d["revenue"] += rev; d["ka"] += cnt; d["ka_revenue"] += rev
        bt = d["by_tariff"].setdefault(str(r["amount"]), {"count": 0, "fp": 0, "ka": 0})
        bt["count"] += cnt; bt["ka"] += cnt

    for r in trial_rows:
        _ensure(r["router_id"])["trial"] = r["cnt"]

    routers = dict(sorted(routers.items()))
    return {
        "from_date": from_date,
        "to_date": to_date,
        "totals": {
            "total_paid":         sum(d["paid"]       for d in routers.values()),
            "total_revenue":      sum(d["revenue"]     for d in routers.values()),
            "freedompay_paid":    sum(d["fp"]          for d in routers.values()),
            "freedompay_revenue": sum(d["fp_revenue"]  for d in routers.values()),
            "kaspi_paid":         sum(d["ka"]          for d in routers.values()),
            "kaspi_revenue":      sum(d["ka_revenue"]  for d in routers.values()),
            "trial_total":        sum(d["trial"]       for d in routers.values()),
        },
        "routers": routers,
    }


@router.get("/api/admin/stats/range")
async def admin_stats_range(request: Request, from_date: str, to_date: str):
    import hmac as _h
    url_token = (request.query_params.get("token") or "").strip()
    if not _has_admin_auth(request):
        if not (url_token and ADMIN_TOKEN and _h.compare_digest(url_token, ADMIN_TOKEN)):
            return utf8_json_response({"error": "Unauthorized"}, status_code=401)
    try:
        from datetime import date as _date
        _date.fromisoformat(from_date)
        _date.fromisoformat(to_date)
    except ValueError:
        return utf8_json_response({"error": "Invalid date format, expected YYYY-MM-DD"}, status_code=400)
    if from_date > to_date:
        from_date, to_date = to_date, from_date
    data = await asyncio.to_thread(_collect_router_stats_range, from_date, to_date)
    return utf8_json_response(data)


@router.get("/health")
async def health_check(request: Request):
    if not _has_admin_auth(request):
        return utf8_json_response({"error": "Unauthorized"}, status_code=401)
    logger.info("🩺 HEALTH CHECK: проверка всех роутеров...")
    health_report = {
        "timestamp": datetime.now(KZ_TZ).isoformat(),
        "routers": {},
    }
    for router_id, config in ROUTERS_CONFIG.items():
        logger.info(f"  Проверяю {router_id} ({config['ip']})...")
        status = {
            "ip": config["ip"],
            "connectivity": False,
            "hotspot_enabled": False,
            "details": "",
        }
        try:
            connection = routeros_api.RouterOsApiPool(
                config["ip"],
                username=config["user"],
                password=config["pass"],
                port=config.get("port", 8728),
                plaintext_login=True,
            )
            api = connection.get_api()
            identity = api.get_resource("/system/identity").call("print")
            if identity:
                status["connectivity"] = True
                status["details"] = f"Identity: {identity[0].get('name', 'N/A')}"
                logger.info(f"    ✓ Связь: OK ({status['details']})")
            hotspot_enabled = check_router_hotspot_enabled(config)
            status["hotspot_enabled"] = hotspot_enabled
            if hotspot_enabled:
                logger.info(f"    ✓ Hotspot: ENABLED")
            else:
                logger.warning(f"    ❌ Hotspot: DISABLED или ошибка конфигурации!")
                status["details"] += " | Hotspot: PROBLEM"
            connection.disconnect()
        except Exception as e:
            logger.error(f"    ❌ Ошибка подключения: {str(e)[:150]}")
            status["details"] = f"Error: {str(e)[:100]}"
        health_report["routers"][router_id] = status
    logger.info("🩺 HEALTH CHECK завершен")
    return utf8_json_response(health_report)


@router.get("/debug")
async def debug_router_status(request: Request, mac: str = "00:00:00:00:00:00", router_id: str = "astana_01"):
    if not _has_admin_auth(request):
        return utf8_json_response({"error": "Unauthorized"}, status_code=401)
    logger.info(f"🔍 DEBUG запрос: MAC={mac}, router={router_id}")
    config = ROUTERS_CONFIG.get(router_id)
    if not config:
        return utf8_json_response({"error": f"Неизвестный router_id: {router_id}"}, status_code=400)
    if not _is_valid_mac(mac or ""):
        return utf8_json_response({"error": f"Некорректный MAC: {mac}"}, status_code=400)

    debug_info = {
        "mac": mac,
        "router_id": router_id,
        "router_ip": config["ip"],
        "bindings": [],
        "active_sessions": [],
        "users": [],
        "schedulers": [],
    }
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
        bindings = binding.call("print", queries={"mac-address": mac})
        for b in bindings:
            debug_info["bindings"].append({"type": b.get("type"), "comment": b.get("comment"), "id": b.get("id")})
        active = api.get_resource("/ip/hotspot/active")
        active_sessions = active.call("print", queries={"mac-address": mac})
        for a in active_sessions:
            debug_info["active_sessions"].append({
                "user": a.get("user"), "address": a.get("address"),
                "uptime": a.get("uptime"), "id": a.get("id"),
            })
        user_res = api.get_resource("/ip/hotspot/user")
        users = user_res.call("print")
        for u in users:
            if mac in (u.get("comment", "") or ""):
                debug_info["users"].append({"name": u.get("name"), "comment": u.get("comment"), "id": u.get("id")})
        sched = api.get_resource("/system/scheduler")
        task_name_pattern = mac.replace(":", "")
        all_schedulers = sched.call("print")
        for s in all_schedulers:
            name = s.get("name", "")
            if task_name_pattern in name:
                debug_info["schedulers"].append({
                    "name": name, "start-date": s.get("start-date"),
                    "start-time": s.get("start-time"), "comment": s.get("comment"), "id": s.get("id"),
                })
        connection.disconnect()
        logger.info(f"🔍 DEBUG: найдено {len(debug_info['bindings'])} биндингов, "
                    f"{len(debug_info['active_sessions'])} активных, "
                    f"{len(debug_info['schedulers'])} schedulers")
    except Exception as e:
        logger.error(f"🔍 DEBUG: ошибка: {str(e)[:150]}")
        return utf8_json_response({"error": str(e)}, status_code=500)

    return utf8_json_response(debug_info)


@router.get("/api/admin/stats")
async def admin_stats_json(request: Request):
    if not _has_admin_auth(request):
        return utf8_json_response({"error": "Unauthorized"}, status_code=401)
    data = await asyncio.to_thread(_collect_router_stats)
    return utf8_json_response(data)


@router.get("/admin/stats", response_class=HTMLResponse)
async def admin_stats_page(request: Request):
    import hmac as _h
    url_token = (request.query_params.get("token") or "").strip()
    if url_token and ADMIN_TOKEN and _h.compare_digest(url_token, ADMIN_TOKEN):
        pass
    elif not _has_admin_auth(request):
        return HTMLResponse("<h2>401 Unauthorized — укажи ?token=ADMIN_TOKEN</h2>", status_code=401)

    data = await asyncio.to_thread(_collect_router_stats)
    tariffs_list = load_tariffs_config()
    return templates.TemplateResponse(
        "admin_stats.html",
        {"request": request, "data": data, "tariffs": tariffs_list},
    )


@router.post("/api/activation/process_pending")
async def process_pending_activations(request: Request, limit: int = 10):
    if not _has_admin_auth(request):
        return utf8_json_response({"error": "Unauthorized"}, status_code=401)
    limit = max(1, min(100, int(limit)))
    processed = await _drain_pending_activations(limit=limit)
    return utf8_json_response({"ok": True, "processed": processed})
