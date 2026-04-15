import json
import logging
from datetime import datetime
from typing import Any
from urllib import error, parse, request


logger = logging.getLogger("WiFiGateway.Kaspi")


class KaspiClientError(Exception):
    pass


class KaspiTransportError(KaspiClientError):
    pass


class KaspiProtocolError(KaspiClientError):
    pass


class KaspiApiClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        orders_path: str = "/orders",
        order_details_path: str = "/orders/{order_id}",
        timeout_sec: int = 10,
    ):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.token = (token or "").strip()
        self.orders_path = orders_path or "/orders"
        self.order_details_path = order_details_path or "/orders/{order_id}"
        self.timeout_sec = max(1, int(timeout_sec))

        if not self.base_url:
            raise KaspiClientError("KASPI_API_BASE_URL is empty")
        if not self.token:
            raise KaspiClientError("KASPI_API_TOKEN is empty")

    def _build_url(self, path: str, query: dict[str, Any] | None = None) -> str:
        full_path = path if path.startswith("/") else f"/{path}"
        url = f"{self.base_url}{full_path}"
        if query:
            clean = {k: v for k, v in query.items() if v is not None and v != ""}
            if clean:
                url = f"{url}?{parse.urlencode(clean, doseq=True)}"
        return url

    def _request_json(self, url: str) -> Any:
        req = request.Request(url=url, method="GET")
        req.add_header("Accept", "application/json")
        req.add_header("X-Auth-Token", self.token)

        try:
            with request.urlopen(req, timeout=self.timeout_sec) as resp:
                status = getattr(resp, "status", 200)
                payload = resp.read().decode("utf-8", errors="replace")
        except error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            logger.error("[KASPI] HTTP error status=%s url=%s body=%s", e.code, url, body[:300])
            raise KaspiTransportError(f"HTTP {e.code}") from e
        except error.URLError as e:
            logger.error("[KASPI] Transport error url=%s reason=%s", url, str(e.reason)[:200])
            raise KaspiTransportError("Network error") from e

        if status < 200 or status >= 300:
            logger.error("[KASPI] Unexpected status=%s url=%s", status, url)
            raise KaspiTransportError(f"Unexpected HTTP status {status}")

        try:
            return json.loads(payload)
        except json.JSONDecodeError as e:
            logger.error("[KASPI] Invalid JSON url=%s body=%s", url, payload[:300])
            raise KaspiProtocolError("Invalid JSON response") from e

    def get_orders(
        self,
        statuses: list[str] | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if statuses:
            query["status"] = statuses
        if created_from:
            query["created_from"] = created_from
        if created_to:
            query["created_to"] = created_to

        url = self._build_url(self.orders_path, query)
        raw = self._request_json(url)
        return self._extract_orders(raw)

    def get_order_details(self, order_id: str) -> dict[str, Any]:
        if not order_id:
            raise KaspiClientError("order_id is required")
        path = self.order_details_path.replace("{order_id}", parse.quote(str(order_id), safe=""))
        url = self._build_url(path)
        raw = self._request_json(url)
        item = self._extract_single_order(raw)
        if not item:
            raise KaspiProtocolError("Order details payload is empty")
        return item

    def _extract_orders(self, payload: Any) -> list[dict[str, Any]]:
        candidates: list[Any] = []
        if isinstance(payload, list):
            candidates = payload
        elif isinstance(payload, dict):
            for key in ("orders", "data", "results", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    candidates = value
                    break
            if not candidates and any(k in payload for k in ("id", "orderId", "status", "contractNumber")):
                candidates = [payload]
        else:
            raise KaspiProtocolError("Unsupported orders payload type")

        normalized = []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            order = self._normalize_order(item)
            if order:
                normalized.append(order)
        return normalized

    def _extract_single_order(self, payload: Any) -> dict[str, Any] | None:
        if isinstance(payload, dict):
            for key in ("order", "data", "result"):
                value = payload.get(key)
                if isinstance(value, dict):
                    return self._normalize_order(value)
            return self._normalize_order(payload)
        if isinstance(payload, list) and payload:
            first = payload[0]
            if isinstance(first, dict):
                return self._normalize_order(first)
        return None

    def _normalize_order(self, item: dict[str, Any]) -> dict[str, Any] | None:
        kaspi_order_id = (
            item.get("id")
            or item.get("orderId")
            or item.get("kaspiOrderId")
            or item.get("order_id")
        )
        contract_number = (
            item.get("contractNumber")
            or item.get("contract_number")
            or item.get("externalOrderRef")
            or item.get("external_order_ref")
            or item.get("reference")
        )
        status = (
            item.get("status")
            or item.get("orderStatus")
            or item.get("state")
            or ""
        )
        amount_raw = item.get("amount") or item.get("totalAmount") or item.get("sum")
        paid_at = item.get("paidAt") or item.get("paid_at") or item.get("paymentDate")
        created_at = item.get("createdAt") or item.get("created_at") or item.get("dateCreated")

        amount = None
        if amount_raw is not None:
            try:
                amount = int(float(amount_raw))
            except Exception:
                amount = None

        if not kaspi_order_id and not contract_number:
            return None

        return {
            "kaspi_order_id": str(kaspi_order_id) if kaspi_order_id is not None else "",
            "contract_number": str(contract_number) if contract_number is not None else "",
            "kaspi_status": str(status).strip().upper(),
            "amount": amount,
            "paid_at": self._normalize_dt(paid_at),
            "created_at": self._normalize_dt(created_at),
            "raw": item,
        }

    @staticmethod
    def _normalize_dt(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text).isoformat()
        except Exception:
            return str(value)
