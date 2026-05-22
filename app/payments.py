"""
app/payments.py — FreedomPay signature and URL helpers.
"""

import hashlib
import secrets
from urllib.parse import urlencode, unquote

from .config import MERCHANT_ID, SECRET_KEY, PAY_URL, BASE_URL, logger
from .utils import make_cid, get_tariff_runtime_state


def get_signature(script_name, params, secret_key):
    sorted_keys = sorted(params.keys())
    values = [str(params[k]) for k in sorted_keys if k != 'pg_sig' and params[k] is not None]
    sig_str = f"{script_name};{';'.join(values)};{secret_key}"
    return hashlib.md5(sig_str.encode('utf-8')).hexdigest()


def decode_nested_url_value(value: str) -> str:
    if value is None:
        return ""
    decoded = value
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def build_payment_url(amount: int, mac: str, router_id: str, payment_order_id: str, cid: str = "") -> str:
    _, amount_to_minutes, _, _ = get_tariff_runtime_state()
    minutes = amount_to_minutes.get(amount, 60)
    cid = (cid or make_cid())[:24]
    success_url = (
        f"{BASE_URL}/success"
        f"?mac={mac}"
        f"&router_id={router_id}"
        f"&minutes={minutes}"
        f"&amount={amount}"
        f"&cid={cid}"
    )
    params = {
        'pg_merchant_id': MERCHANT_ID, 'pg_amount': str(amount), 'pg_currency': 'KZT',
        'pg_description': f"Wi-Fi {mac}", 'pg_order_id': payment_order_id,
        'pg_salt': 'salt', 'pg_param1': mac, 'pg_param2': router_id, 'pg_param3': cid,
        'pg_result_url': f'{BASE_URL}/payment_result',
        'pg_success_url': success_url,
    }
    params['pg_sig'] = get_signature("payment.php", params, SECRET_KEY)
    return f"{PAY_URL}?{urlencode(params)}"
