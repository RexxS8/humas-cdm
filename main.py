from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from db.database import engine, Base
from api.routes import router as api_router
from api.websocket import manager
from dotenv import load_dotenv
import os
from sqlalchemy import text

load_dotenv()

# Create DB tables
Base.metadata.create_all(bind=engine)

# Auto-migration for existing SQLite database
with engine.connect() as conn:
    try:
        conn.execute(text("ALTER TABLE chat_messages ADD COLUMN msg_type VARCHAR DEFAULT 'text'"))
    except Exception:
        pass
    try:
        conn.execute(text("ALTER TABLE chat_messages ADD COLUMN media_url VARCHAR"))
    except Exception:
        pass
    try:
        conn.execute(text("ALTER TABLE contacts ADD COLUMN label VARCHAR"))
    except Exception:
        pass
    try:
        conn.execute(text("ALTER TABLE contacts ADD COLUMN unread_count INTEGER DEFAULT 0"))
    except Exception:
        pass
    try:
        conn.execute(text("ALTER TABLE contacts ADD COLUMN last_message VARCHAR"))
    except Exception:
        pass
    try:
        conn.execute(text("ALTER TABLE contacts ADD COLUMN last_blasted_at DATETIME"))
    except Exception:
        pass
    try:
        conn.execute(text("ALTER TABLE chat_messages ADD COLUMN error_detail VARCHAR"))
    except Exception:
        pass
    conn.commit()



app = FastAPI(title="WhatsApp Omnichannel")

os.makedirs("templates", exist_ok=True)
os.makedirs("static/uploads", exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

app.include_router(api_router)

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse("static/Profile-Pic.png")

@app.get("/")
async def dashboard(request: Request):
    response = templates.TemplateResponse(request=request, name="index.html")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.websocket("/ws/dashboard")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # We don't expect messages from the dashboard, but we need to keep connection open
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

