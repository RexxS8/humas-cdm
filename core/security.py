import os
import random
import string
import secrets
from datetime import datetime, timedelta
from typing import Optional
from fastapi import Request, HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from jose import JWTError, jwt
from passlib.context import CryptContext
import pyotp
import httpx
import bcrypt
import logging

from core.config import settings, get_jakarta_now
from db.database import get_db

logger = logging.getLogger(__name__)

# Password Hashing using direct bcrypt
def hash_password(password: str) -> str:
    """Hash a plain text password using bcrypt."""
    pwd_bytes = password.encode('utf-8')[:72]
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(pwd_bytes, salt).decode('utf-8')


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain text password against its hash."""
    try:
        pwd_bytes = plain_password.encode('utf-8')[:72]
        hash_bytes = hashed_password.encode('utf-8')
        return bcrypt.checkpw(pwd_bytes, hash_bytes)
    except Exception:
        return False


def create_jwt_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT token with custom claims and expiration."""
    to_encode = data.copy()
    if expires_delta:
        expire = get_jakarta_now() + expires_delta
    else:
        expire = get_jakarta_now() + timedelta(hours=settings.SESSION_EXPIRE_HOURS)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def decode_jwt_token(token: str) -> Optional[dict]:
    """Decode and validate a JWT token."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except JWTError as e:
        logger.warning(f"JWT decode error: {e}")
        return None


def generate_otp_code(length: int = 6) -> str:
    """Generate a random N-digit numeric OTP code."""
    return "".join(random.choices(string.digits, k=length))


def generate_totp_secret() -> str:
    """Generate a random secret key for Google Authenticator TOTP."""
    return pyotp.random_base32()


def verify_totp_code(secret: str, code: str) -> bool:
    """Verify a 6-digit code against Google Authenticator TOTP secret."""
    if not secret or not code:
        return False
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)


def get_client_ip(request: Request) -> str:
    """Extract real client IP address considering proxy headers."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
        if ip:
            return ip
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


async def get_geoip_location(ip_address: str) -> str:
    """Fetch city/country location using GeoIP API for login alert."""
    if ip_address in ("127.0.0.1", "localhost", "::1") or ip_address.startswith("192.168.") or ip_address.startswith("10."):
        return "Lokal (Development)"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"http://ip-api.com/json/{ip_address}?fields=status,city,regionName,country,isp")
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "success":
                    city = data.get("city", "")
                    region = data.get("regionName", "")
                    country = data.get("country", "")
                    isp = data.get("isp", "")
                    loc = ", ".join(filter(None, [city, region, country]))
                    if isp:
                        loc += f" ({isp})"
                    return loc if loc else "Lokasi Tidak Diketahui"
    except Exception as e:
        logger.error(f"GeoIP error for {ip_address}: {e}")
    return "Lokasi Tidak Diketahui"


def get_user_agent_summary(request: Request) -> str:
    """Parses User-Agent header into readable device/browser summary."""
    ua = request.headers.get("User-Agent", "")
    if not ua:
        return "Browser Tidak Dikenal"
    
    os_name = "Perangkat Tidak Dikenal"
    if "Windows" in ua:
        os_name = "Windows"
    elif "Macintosh" in ua or "Mac OS" in ua:
        os_name = "macOS"
    elif "Android" in ua:
        os_name = "Android"
    elif "iPhone" in ua or "iPad" in ua:
        os_name = "iOS"
    elif "Linux" in ua:
        os_name = "Linux"

    browser = "Browser"
    if "Edg" in ua:
        browser = "Microsoft Edge"
    elif "Chrome" in ua:
        browser = "Chrome"
    elif "Firefox" in ua:
        browser = "Firefox"
    elif "Safari" in ua and "Chrome" not in ua:
        browser = "Safari"

    return f"{browser} di {os_name}"


def get_current_session_token(request: Request) -> Optional[str]:
    """Retrieves session token from cookies or Authorization header."""
    cookie_token = request.cookies.get("session_token")
    if cookie_token:
        return cookie_token
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header.replace("Bearer ", "").strip()
    return None
