import sqlite3
import logging
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# --- НАСТРОЙКИ ---
DB_NAME = "gateway.db"
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Wi-Fi Gateway")

# Указываем путь к папке с HTML
templates = Jinja2Templates(directory="templates")

# --- БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mac_address TEXT,
                amount INTEGER,
                status TEXT DEFAULT 'WAITING',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
init_db()

# --- МАРШРУТЫ ---

# Главная страница портала
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, mac: str = "AA:BB:CC:DD:EE:FF"):
    # Передаем mac в шаблон index.html
    return templates.TemplateResponse("index.html", {"request": request, "mac": mac})

# Тестовый эндпоинт для проверки связи с роутером (будущий этап)
@app.get("/status")
def get_status():
    return {"status": "online", "server": "FastAPI"}