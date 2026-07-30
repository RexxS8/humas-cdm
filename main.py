from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from db.database import engine, Base, SessionLocal
from db.models import User, UserSession
from api.routes import router as api_router
from api.auth import router as auth_router
from api.websocket import manager
from core.config import settings, get_jakarta_now
from core.security import decode_jwt_token, get_current_session_token, hash_password
from dotenv import load_dotenv
import os
from sqlalchemy import text

load_dotenv()

# Create DB tables
Base.metadata.create_all(bind=engine)

# Auto-migration for existing SQLite database (Preserving existing data)
with engine.connect() as conn:
    for sql in [
        "ALTER TABLE chat_messages ADD COLUMN msg_type VARCHAR DEFAULT 'text'",
        "ALTER TABLE chat_messages ADD COLUMN media_url VARCHAR",
        "ALTER TABLE contacts ADD COLUMN label VARCHAR",
        "ALTER TABLE contacts ADD COLUMN unread_count INTEGER DEFAULT 0",
        "ALTER TABLE contacts ADD COLUMN last_message VARCHAR",
        "ALTER TABLE contacts ADD COLUMN last_blasted_at DATETIME",
        "ALTER TABLE chat_messages ADD COLUMN error_detail VARCHAR",
        "ALTER TABLE users ADD COLUMN role VARCHAR DEFAULT 'superadmin'",
        "ALTER TABLE users ADD COLUMN session_expire_hours INTEGER DEFAULT 24"
    ]:
        try:
            conn.execute(text(sql))
        except Exception:
            pass
    conn.commit()

# Seed default Admin user if not exists
with SessionLocal() as db:
    existing_admin = db.query(User).filter(User.username == settings.ADMIN_USERNAME).first()
    if not existing_admin:
        default_admin = User(
            username=settings.ADMIN_USERNAME,
            hashed_password=hash_password(settings.ADMIN_PASSWORD),
            wa_number=settings.ADMIN_WA_NUMBER,
            email="andri@vihara.org",
            role="superadmin",
            session_expire_hours=settings.SESSION_EXPIRE_HOURS,
            is_active=True
        )
        db.add(default_admin)
        db.commit()
        print(f"\n✅ [AUTH SEED] Super Admin '{settings.ADMIN_USERNAME}' (WA: {settings.ADMIN_WA_NUMBER}) registered.\n")
    elif not existing_admin.role:
        existing_admin.role = "superadmin"
        db.commit()
    
    # Enforce pure WhatsApp OTP 2FA by clearing any TOTP secrets
    db.query(User).update({"totp_secret": None})
    db.commit()

app = FastAPI(title="WhatsApp Omnichannel")

os.makedirs("templates", exist_ok=True)
os.makedirs("static/uploads", exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Register API routers
app.include_router(api_router)
app.include_router(auth_router)

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse("static/Profile-Pic.png")

# Page Routes with Auth Guard

def is_authenticated(request: Request) -> bool:
    """Helper to check if request contains a valid 24h session token."""
    token = get_current_session_token(request)
    if not token:
        return False
    payload = decode_jwt_token(token)
    if not payload or payload.get("type") != "session":
        return False
    
    st = payload.get("session_token")
    with SessionLocal() as db:
        user_sess = db.query(UserSession).filter(
            UserSession.session_token == st,
            UserSession.is_active == True
        ).first()
        if not user_sess or user_sess.expires_at < get_jakarta_now():
            return False
        
        # Verify that the user exists and is active
        user = db.query(User).filter(User.id == user_sess.user_id, User.is_active == True).first()
        if not user:
            user_sess.is_active = False
            db.commit()
            return False

    return True


@app.get("/login")
async def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html")

@app.get("/2fa")
async def fa_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request=request, name="2fa.html")

@app.get("/setup-2fa")
async def setup_2fa_page(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request=request, name="setup_2fa.html")

@app.get("/")
async def dashboard(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    
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
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
