"""
app/routes/misc.py — Static page routes and client_event.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..config import templates, logger
from ..utils import utf8_json_response

router = APIRouter()


@router.get("/payment_methods", response_class=HTMLResponse)
@router.get("/payment_methods.html", response_class=HTMLResponse)
async def payment_methods_page(request: Request, mac: str = "00:00:00:00:00:00", router_id: str = "astana_01"):
    return templates.TemplateResponse("payment_methods.html", {"request": request, "mac": mac, "router_id": router_id})


@router.get("/offer", response_class=HTMLResponse)
@router.get("/offer.html", response_class=HTMLResponse)
async def offer_page(request: Request, mac: str = "00:00:00:00:00:00", router_id: str = "astana_01"):
    return templates.TemplateResponse("offer.html", {"request": request, "mac": mac, "router_id": router_id})


@router.get("/privacy", response_class=HTMLResponse)
@router.get("/privacy.html", response_class=HTMLResponse)
async def privacy_page(request: Request, mac: str = "00:00:00:00:00:00", router_id: str = "astana_01"):
    return templates.TemplateResponse("privacy.html", {"request": request, "mac": mac, "router_id": router_id})


@router.get("/landing", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse("landing.html", {"request": request})


@router.post("/client_event")
async def client_event(request: Request):
    try:
        data = await request.json()
    except Exception:
        return utf8_json_response({"ok": False, "error": "invalid json"}, status_code=400)

    cid = str(data.get("cid") or "-")[:24]
    stage = str(data.get("stage") or "-")[:64]
    page = str(data.get("page") or "-")[:64]
    elapsed_ms = data.get("elapsed_ms")
    extra = data.get("extra")
    logger.info(f"[CLIENT] cid={cid} page={page} stage={stage} elapsed_ms={elapsed_ms} extra={extra}")
    return utf8_json_response({"ok": True})
