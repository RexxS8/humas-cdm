import uuid
import qrcode
import io
import base64
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
import pyotp
import logging

from db.database import get_db
from db.models import User, UserSession, OTPCode
from core.config import settings, get_jakarta_now
from core.security import (
    hash_password,
    verify_password,
    create_jwt_token,
    decode_jwt_token,
    generate_otp_code,
    generate_totp_secret,
    verify_totp_code,
    get_client_ip,
    get_geoip_location,
    get_user_agent_summary,
    get_current_session_token,
)
from services.auth_notif import send_2fa_alert_and_otp
from api.websocket import manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# --- Request Models ---

class LoginRequest(BaseModel):
    username: str
    password: str

class Verify2FARequest(BaseModel):
    pre_2fa_token: str
    code: str

class Resend2FARequest(BaseModel):
    pre_2fa_token: str

class UpdateProfileRequest(BaseModel):
    new_username: Optional[str] = None
    current_password: Optional[str] = None
    new_password: Optional[str] = None
    wa_number: Optional[str] = None
    email: Optional[str] = None


class EnableTOTPRequest(BaseModel):
    secret: str
    code: str

class RevokeSessionRequest(BaseModel):
    session_id: int

# --- Helper to get authenticated user ---

async def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = get_current_session_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Silakan login terlebih dahulu",
        )
    
    payload = decode_jwt_token(token)
    if not payload or payload.get("type") != "session":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sesi tidak valid atau telah kadaluarsa",
        )
    
    user_id = int(payload.get("sub"))
    session_token = payload.get("session_token")
    
    # Check session in database
    user_session = db.query(UserSession).filter(
        UserSession.session_token == session_token,
        UserSession.is_active == True
    ).first()
    
    if not user_session or user_session.expires_at < get_jakarta_now():
        if user_session:
            user_session.is_active = False
            db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sesi Anda telah berakhir (expired 24 jam). Silakan login kembali.",
        )
    
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Pengguna tidak ditemukan",
        )
    
    return user


# --- Endpoints ---

@router.post("/login")
async def login(req: LoginRequest, request: Request, db: Session = Depends(get_db)):
    """Lapis 1: Username & Password Verification."""
    user = db.query(User).filter(User.username == req.username.strip()).first()
    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Username atau Password salah",
        )
    
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Akun Anda telah dinonaktifkan oleh administrator.",
        )

    
    # Create temporary Pre-2FA Token (expires in 5 minutes)
    pre_2fa_token = str(uuid.uuid4())
    pre_2fa_jwt = create_jwt_token(
        data={"sub": str(user.id), "type": "pre_2fa", "pre_token": pre_2fa_token},
        expires_delta=timedelta(minutes=settings.OTP_EXPIRE_MINUTES)
    )

    # Generate 6-digit OTP
    otp_code = generate_otp_code(6)
    expires_at = get_jakarta_now() + timedelta(minutes=settings.OTP_EXPIRE_MINUTES)
    
    # Invalidate previous OTPs for this user
    db.query(OTPCode).filter(OTPCode.user_id == user.id, OTPCode.is_used == False).update({"is_used": True})
    
    new_otp = OTPCode(
        user_id=user.id,
        code=otp_code,
        purpose="login_2fa",
        expires_at=expires_at
    )
    db.add(new_otp)
    db.commit()

    # Capture metadata
    client_ip = get_client_ip(request)
    location_str = await get_geoip_location(client_ip)
    device_str = get_user_agent_summary(request)

    # Dispatch WhatsApp 2FA Alert & OTP
    target_wa = user.wa_number or settings.ADMIN_WA_NUMBER
    await send_2fa_alert_and_otp(
        to_phone=target_wa,
        otp_code=otp_code,
        ip_address=client_ip,
        location_str=location_str,
        device_str=device_str,
        username=user.username
    )

    return {
        "status": "2fa_required",
        "message": f"Kode OTP telah dikirimkan ke WhatsApp {target_wa[:5]}***{target_wa[-3:]}",
        "pre_2fa_token": pre_2fa_jwt,
        "totp_enabled": bool(user.totp_secret),
        "expires_in_seconds": settings.OTP_EXPIRE_MINUTES * 60
    }


@router.post("/2fa/verify")
async def verify_2fa(
    req: Verify2FARequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db)
):
    """Lapis 2: Verify 6-digit OTP or TOTP code."""
    payload = decode_jwt_token(req.pre_2fa_token)
    if not payload or payload.get("type") != "pre_2fa":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Sesi verifikasi 2FA telah kadaluarsa. Silakan login ulang.",
        )
    
    user_id = payload.get("sub")
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        raise HTTPException(status_code=404, detail="User tidak ditemukan")

    clean_code = req.code.strip()
    is_valid = False

    # Check WhatsApp OTP Code
    otp_record = db.query(OTPCode).filter(
        OTPCode.user_id == user.id,
        OTPCode.code == clean_code,
        OTPCode.is_used == False,
        OTPCode.expires_at >= get_jakarta_now()
    ).order_by(OTPCode.id.desc()).first()

    if not otp_record:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Kode OTP WhatsApp salah atau telah kadaluarsa",
        )

    otp_record.is_used = True
    db.commit()


    # 2FA Success -> Create Active Session with user's configured lifetime
    expire_hours = user.session_expire_hours if user.session_expire_hours else settings.SESSION_EXPIRE_HOURS
    session_token = str(uuid.uuid4())
    expires_at = get_jakarta_now() + timedelta(hours=expire_hours)
    client_ip = get_client_ip(request)
    location_str = await get_geoip_location(client_ip)
    device_str = get_user_agent_summary(request)

    user_session = UserSession(
        user_id=user.id,
        session_token=session_token,
        ip_address=client_ip,
        user_agent=device_str,
        location=location_str,
        expires_at=expires_at,
        is_active=True
    )
    db.add(user_session)
    db.commit()

    # Generate Session JWT
    jwt_token = create_jwt_token(
        data={"sub": str(user.id), "type": "session", "session_token": session_token},
        expires_delta=timedelta(hours=expire_hours)
    )

    # Set Cookie for Browser Session
    response.set_cookie(
        key="session_token",
        value=jwt_token,
        httponly=True,
        max_age=expire_hours * 3600,
        samesite="lax"
    )

    return {
        "status": "success",
        "message": f"Login berhasil! Akses berlaku selama {expire_hours} jam.",
        "session_token": jwt_token,
        "expires_at": expires_at.isoformat(),
        "user": {
            "id": user.id,
            "username": user.username,
            "role": user.role or "superadmin",
            "wa_number": user.wa_number,
            "email": user.email
        }
    }


@router.post("/2fa/resend")
async def resend_2fa(req: Resend2FARequest, request: Request, db: Session = Depends(get_db)):
    """Resend 2FA OTP Code to WhatsApp."""
    payload = decode_jwt_token(req.pre_2fa_token)
    if not payload or payload.get("type") != "pre_2fa":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Sesi verifikasi telah kadaluarsa. Silakan login ulang.",
        )
    
    user_id = payload.get("sub")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User tidak ditemukan")

    otp_code = generate_otp_code(6)
    expires_at = get_jakarta_now() + timedelta(minutes=settings.OTP_EXPIRE_MINUTES)

    db.query(OTPCode).filter(OTPCode.user_id == user.id, OTPCode.is_used == False).update({"is_used": True})
    
    new_otp = OTPCode(
        user_id=user.id,
        code=otp_code,
        purpose="login_2fa",
        expires_at=expires_at
    )
    db.add(new_otp)
    db.commit()

    client_ip = get_client_ip(request)
    location_str = await get_geoip_location(client_ip)
    device_str = get_user_agent_summary(request)

    target_wa = user.wa_number or settings.ADMIN_WA_NUMBER
    await send_2fa_alert_and_otp(
        to_phone=target_wa,
        otp_code=otp_code,
        ip_address=client_ip,
        location_str=location_str,
        device_str=device_str,
        username=user.username
    )

    return {"status": "success", "message": "Kode OTP baru berhasil dikirim ulang ke WhatsApp."}


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db)
):
    """Logout & invalidate active user session."""
    token = get_current_session_token(request)
    if token:
        payload = decode_jwt_token(token)
        if payload and payload.get("session_token"):
            st = payload.get("session_token")
            db.query(UserSession).filter(UserSession.session_token == st).update({"is_active": False})
            db.commit()

    response.delete_cookie(key="session_token")
    return {"status": "success", "message": "Logout berhasil"}


@router.get("/me")
async def get_me(current_user: User = Depends(get_current_user)):
    """Get profile info of current logged-in user."""
    return {
        "id": current_user.id,
        "username": current_user.username,
        "role": current_user.role or "superadmin",
        "wa_number": current_user.wa_number,
        "email": current_user.email,
        "session_expire_hours": current_user.session_expire_hours or 24,
        "totp_enabled": bool(current_user.totp_secret),
        "created_at": current_user.created_at.isoformat() if current_user.created_at else None
    }


@router.post("/update-profile")
async def update_profile(
    req: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Allow logged-in user to change Username & Password!
    (Memungkinkan pengubah Username & Password setelah login).
    """
    is_superadmin = (current_user.role or "subadmin") == "superadmin"
    is_changing_password = bool(req.new_password and len(req.new_password.strip()) >= 6)

    # Sub-admin is forbidden from changing password
    if is_changing_password and not is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akses ditolak: Ganti password akun Sub-Admin hanya dapat dilakukan oleh Super Admin."
        )

    # Require current_password ONLY if Super Admin is changing their password, OR if current_password was provided
    if is_changing_password or (req.current_password and req.current_password.strip()):
        if not req.current_password or not verify_password(req.current_password, current_user.hashed_password):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Password lama/saat ini salah",
            )

    updated_fields = []

    # Update username if provided
    if req.new_username and req.new_username.strip() != current_user.username:
        existing = db.query(User).filter(User.username == req.new_username.strip(), User.id != current_user.id).first()
        if existing:
            raise HTTPException(status_code=400, detail="Username tersebut sudah digunakan oleh user lain")
        current_user.username = req.new_username.strip()
        updated_fields.append("Username")

    # Update password if provided
    if is_changing_password:
        current_user.hashed_password = hash_password(req.new_password.strip())
        updated_fields.append("Password")



    # Update WA Number if provided
    if req.wa_number:
        current_user.wa_number = req.wa_number.strip()
        updated_fields.append("Nomor WhatsApp")

    # Update Email if provided
    if req.email:
        current_user.email = req.email.strip()
        updated_fields.append("Email")

    db.commit()
    db.refresh(current_user)

    return {
        "status": "success",
        "message": f"Berhasil memperbarui: {', '.join(updated_fields)}" if updated_fields else "Tidak ada perubahan",
        "user": {
            "username": current_user.username,
            "wa_number": current_user.wa_number,
            "email": current_user.email
        }
    }


@router.get("/sessions")
async def get_active_sessions(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Retrieve list of active login sessions for Session Management."""
    sessions = db.query(UserSession).filter(
        UserSession.user_id == current_user.id,
        UserSession.is_active == True,
        UserSession.expires_at >= get_jakarta_now()
    ).order_by(UserSession.created_at.desc()).all()

    result = []
    for s in sessions:
        result.append({
            "id": s.id,
            "ip_address": s.ip_address,
            "location": s.location or "Tidak diketahui",
            "user_agent": s.user_agent or "Browser",
            "created_at": s.created_at.strftime("%d %b %Y, %H:%M WIB"),
            "expires_at": s.expires_at.strftime("%d %b %Y, %H:%M WIB")
        })
    return {"sessions": result}


@router.post("/sessions/revoke")
async def revoke_session(
    req: RevokeSessionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Revoke (Kick Out) a specific active session."""
    session_obj = db.query(UserSession).filter(
        UserSession.id == req.session_id,
        UserSession.user_id == current_user.id
    ).first()

    if not session_obj:
        raise HTTPException(status_code=404, detail="Sesi tidak ditemukan")

    session_obj.is_active = False
    db.commit()
    return {"status": "success", "message": "Sesi berhasil dicabut."}


@router.post("/sessions/revoke-all")
async def revoke_all_sessions(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Remote Kill-Switch: Revoke ALL active sessions for this user."""
    db.query(UserSession).filter(
        UserSession.user_id == current_user.id,
        UserSession.is_active == True
    ).update({"is_active": False})
    db.commit()
    return {"status": "success", "message": "Seluruh sesi aktif berhasil dibatalkan."}


@router.get("/setup-2fa")
async def setup_2fa(current_user: User = Depends(get_current_user)):
    """Generate TOTP Secret & QR Code for Google Authenticator (Super Admin only)."""
    if (current_user.role or "subadmin") != "superadmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akses ditolak: Pengaturan 2FA hanya dapat dikonfigurasi oleh Super Admin."
        )

    secret = generate_totp_secret()
    totp = pyotp.TOTP(secret)
    provisioning_uri = totp.provisioning_uri(
        name=current_user.username,
        issuer_name="WA-CDM Omnichannel"
    )

    # Generate QR Code image as Base64 string
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(provisioning_uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    qr_b64 = base64.b64encode(buffered.getvalue()).decode()

    return {
        "secret": secret,
        "qr_code_base64": f"data:image/png;base64,{qr_b64}",
        "otpauth_url": provisioning_uri
    }


@router.post("/setup-2fa/enable")
async def enable_2fa(
    req: EnableTOTPRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Enable Google Authenticator TOTP after verifying test code (Super Admin only)."""
    if (current_user.role or "subadmin") != "superadmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akses ditolak: Pengaturan 2FA hanya dapat dikonfigurasi oleh Super Admin."
        )

    if not verify_totp_code(req.secret, req.code.strip()):
        raise HTTPException(status_code=400, detail="Kode verifikasi TOTP tidak valid")

    current_user.totp_secret = req.secret
    db.commit()
    return {"status": "success", "message": "Google Authenticator TOTP berhasil diaktifkan!"}


# --- Sub-Admin & User Management Endpoints (Super Admin Only) ---

class CreateSubUserRequest(BaseModel):
    username: str
    password: str
    wa_number: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = "subadmin"
    session_expire_hours: Optional[int] = 24

class UpdateSubUserRequest(BaseModel):
    password: Optional[str] = None
    wa_number: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    session_expire_hours: Optional[int] = None
    is_active: Optional[bool] = None
    reset_totp: Optional[bool] = None


def require_superadmin(current_user: User = Depends(get_current_user)):
    if (current_user.role or "superadmin") != "superadmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akses ditolak: Fitur ini khusus untuk Super Admin",
        )
    return current_user


@router.get("/users")
async def list_users(
    admin_user: User = Depends(require_superadmin),
    db: Session = Depends(get_db)
):
    """List all registered admin users (Super Admin only)."""
    users = db.query(User).order_by(User.id.asc()).all()
    result = []
    for u in users:
        result.append({
            "id": u.id,
            "username": u.username,
            "role": u.role or "superadmin",
            "wa_number": u.wa_number or settings.ADMIN_WA_NUMBER,
            "email": u.email,
            "session_expire_hours": u.session_expire_hours or 24,
            "is_active": u.is_active,
            "totp_enabled": bool(u.totp_secret),
            "created_at": u.created_at.strftime("%d %b %Y, %H:%M WIB") if u.created_at else None
        })
    return {"users": result}


@router.post("/users")
async def create_user(
    req: CreateSubUserRequest,
    admin_user: User = Depends(require_superadmin),
    db: Session = Depends(get_db)
):
    """Create a new Sub-Admin or Admin user (Super Admin only)."""
    username_clean = req.username.strip()
    if not username_clean or len(req.password.strip()) < 6:
        raise HTTPException(status_code=400, detail="Username wajib diisi dan Password minimal 6 karakter")

    existing = db.query(User).filter(User.username == username_clean).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Username '{username_clean}' sudah digunakan")

    new_user = User(
        username=username_clean,
        hashed_password=hash_password(req.password.strip()),
        wa_number=req.wa_number.strip() if req.wa_number else settings.ADMIN_WA_NUMBER,
        email=req.email.strip() if req.email else None,
        role=req.role if req.role in ["superadmin", "subadmin"] else "subadmin",
        session_expire_hours=req.session_expire_hours if req.session_expire_hours else 24,
        is_active=True
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    return {
        "status": "success",
        "message": f"Berhasil menambahkan {new_user.role.upper()}: '{new_user.username}'",
        "user": {
            "id": new_user.id,
            "username": new_user.username,
            "role": new_user.role,
            "wa_number": new_user.wa_number
        }
    }


@router.put("/users/{user_id}")
async def update_user(
    user_id: int,
    req: UpdateSubUserRequest,
    admin_user: User = Depends(require_superadmin),
    db: Session = Depends(get_db)
):
    """Update Sub-Admin data (password, wa_number, role, expire hours, status, reset TOTP)."""
    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User tidak ditemukan")

    if req.password and len(req.password.strip()) >= 6:
        target_user.hashed_password = hash_password(req.password.strip())
    
    if req.wa_number is not None:
        target_user.wa_number = req.wa_number.strip()

    if req.email is not None:
        target_user.email = req.email.strip()

    if req.role in ["superadmin", "subadmin"]:
        target_user.role = req.role

    if req.session_expire_hours and req.session_expire_hours > 0:
        target_user.session_expire_hours = req.session_expire_hours

    if req.is_active is not None:
        target_user.is_active = req.is_active
        if not req.is_active:
            db.query(UserSession).filter(UserSession.user_id == user_id).update({"is_active": False})
            await manager.broadcast({"type": "session_revoked", "user_id": user_id})

    if req.reset_totp:
        target_user.totp_secret = None

    db.commit()
    return {"status": "success", "message": f"Akun '{target_user.username}' berhasil diperbarui"}


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    permanent: bool = True,
    admin_user: User = Depends(require_superadmin),
    db: Session = Depends(get_db)
):
    """Delete (permanently) or deactivate a Sub-Admin user (Super Admin only)."""
    if user_id == admin_user.id:
        raise HTTPException(status_code=400, detail="Tidak dapat menghapus akun Anda sendiri")

    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User tidak ditemukan")

    username = target_user.username

    if permanent:
        db.query(UserSession).filter(UserSession.user_id == user_id).delete(synchronize_session=False)
        db.query(OTPCode).filter(OTPCode.user_id == user_id).delete(synchronize_session=False)
        db.delete(target_user)
        db.commit()
        await manager.broadcast({"type": "session_revoked", "user_id": user_id})
        return {"status": "success", "message": f"Akun Sub-Admin '{username}' telah dihapus secara permanen dari database."}
    else:
        target_user.is_active = False
        db.query(UserSession).filter(UserSession.user_id == user_id).update({"is_active": False})
        db.commit()
        await manager.broadcast({"type": "session_revoked", "user_id": user_id})
        return {"status": "success", "message": f"Akun Sub-Admin '{username}' telah dinonaktifkan."}


