"""
app/db.py — SQLite connection pool, get_db(), init_db().
"""

import sqlite3
from queue import Queue, Empty

from .config import DB_PATH, logger

_DB_POOL_MAX = 8
_db_pool: "Queue[sqlite3.Connection]" = Queue(maxsize=_DB_POOL_MAX)


class _PooledConn:
    """Прозрачный прокси sqlite3.Connection: при close() возвращается в пул."""
    __slots__ = ("_raw",)

    def __init__(self, raw: sqlite3.Connection) -> None:
        object.__setattr__(self, "_raw", raw)

    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, "_raw"), name)

    def __setattr__(self, name: str, value) -> None:
        if name == "_raw":
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_raw"), name, value)

    def close(self) -> None:
        raw = object.__getattribute__(self, "_raw")
        raw.row_factory = None
        try:
            raw.rollback()
        except Exception:
            pass
        try:
            _db_pool.put_nowait(raw)
        except Exception:
            raw.close()


def _create_raw_db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def get_db():
    """Возвращает соединение из пула (или создаёт новое).
    Caller обязан вызвать conn.close() — соединение вернётся в пул."""
    try:
        raw = _db_pool.get_nowait()
    except Empty:
        raw = _create_raw_db_conn()
    return _PooledConn(raw)


def init_db():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mac_address TEXT,
            amount INTEGER,
            status TEXT,
            router_id TEXT,
            device_id TEXT,
            payment_order_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(orders)")
    columns = {row[1] for row in cursor.fetchall()}
    if 'device_id' not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN device_id TEXT")
    if 'payment_order_id' not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN payment_order_id TEXT")
    if 'expires_at' not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN expires_at TIMESTAMP")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_mac_router "
        "ON orders(mac_address, router_id, status, created_at DESC)"
    )

    conn.execute('''
        CREATE TABLE IF NOT EXISTS kaspi_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            local_order_id TEXT,
            contract_number TEXT,
            external_order_ref TEXT,
            mac_address TEXT NOT NULL,
            router_id TEXT NOT NULL,
            amount INTEGER NOT NULL,
            minutes INTEGER NOT NULL,
            kaspi_order_id TEXT,
            kaspi_status TEXT,
            paid_at TIMESTAMP,
            activated_at TIMESTAMP,
            is_activated INTEGER NOT NULL DEFAULT 0,
            activation_lock INTEGER NOT NULL DEFAULT 0,
            activation_attempts INTEGER NOT NULL DEFAULT 0,
            activation_error TEXT,
            last_activation_attempt_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_kaspi_orders_contract_number ON kaspi_orders(contract_number)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_kaspi_orders_kaspi_order_id ON kaspi_orders(kaspi_order_id) WHERE kaspi_order_id IS NOT NULL AND kaspi_order_id <> ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kaspi_orders_status ON kaspi_orders(kaspi_status, is_activated)")

    conn.execute('''
        CREATE TABLE IF NOT EXISTS router_clients_seen (
            router_id TEXT NOT NULL,
            mac_address TEXT NOT NULL,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            seen_count INTEGER NOT NULL DEFAULT 1,
            source TEXT DEFAULT 'router_poll',
            PRIMARY KEY (router_id, mac_address)
        )
    ''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_router_clients_seen_router_time ON router_clients_seen(router_id, last_seen DESC)")

    conn.execute('''
        CREATE TABLE IF NOT EXISTS pending_activations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            router_id TEXT NOT NULL,
            mac_address TEXT NOT NULL,
            amount INTEGER NOT NULL DEFAULT 0,
            minutes INTEGER NOT NULL DEFAULT 0,
            payment_order_id TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            last_attempt_at TIMESTAMP,
            activated_at TIMESTAMP,
            next_retry_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_activations_status ON pending_activations(status, router_id, created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_activations_retry ON pending_activations(status, next_retry_at, created_at)")

    conn.execute('''
        CREATE TABLE IF NOT EXISTS phone_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            mac_address TEXT NOT NULL,
            router_id TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(phone)
        )
    ''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_phone_sessions_phone ON phone_sessions(phone)")

    cursor.execute("PRAGMA table_info(orders)")
    o_columns = {row[1] for row in cursor.fetchall()}
    if 'phone' not in o_columns:
        conn.execute("ALTER TABLE orders ADD COLUMN phone TEXT")

    cursor.execute("PRAGMA table_info(kaspi_orders)")
    k_columns = {row[1] for row in cursor.fetchall()}
    if 'external_order_ref' not in k_columns:
        conn.execute("ALTER TABLE kaspi_orders ADD COLUMN external_order_ref TEXT")
    if 'activation_lock' not in k_columns:
        conn.execute("ALTER TABLE kaspi_orders ADD COLUMN activation_lock INTEGER NOT NULL DEFAULT 0")
    if 'activation_attempts' not in k_columns:
        conn.execute("ALTER TABLE kaspi_orders ADD COLUMN activation_attempts INTEGER NOT NULL DEFAULT 0")
    if 'activation_error' not in k_columns:
        conn.execute("ALTER TABLE kaspi_orders ADD COLUMN activation_error TEXT")
    if 'last_activation_attempt_at' not in k_columns:
        conn.execute("ALTER TABLE kaspi_orders ADD COLUMN last_activation_attempt_at TIMESTAMP")
    if 'phone' not in k_columns:
        conn.execute("ALTER TABLE kaspi_orders ADD COLUMN phone TEXT")

    cursor.execute("PRAGMA table_info(pending_activations)")
    p_columns = {row[1] for row in cursor.fetchall()}
    if 'attempts' not in p_columns:
        conn.execute("ALTER TABLE pending_activations ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
    if 'last_error' not in p_columns:
        conn.execute("ALTER TABLE pending_activations ADD COLUMN last_error TEXT")
    if 'last_attempt_at' not in p_columns:
        conn.execute("ALTER TABLE pending_activations ADD COLUMN last_attempt_at TIMESTAMP")
    if 'activated_at' not in p_columns:
        conn.execute("ALTER TABLE pending_activations ADD COLUMN activated_at TIMESTAMP")
    if 'next_retry_at' not in p_columns:
        conn.execute("ALTER TABLE pending_activations ADD COLUMN next_retry_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")

    conn.commit()
    conn.close()
