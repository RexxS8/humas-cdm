from pydantic_settings import BaseSettings
from datetime import datetime, timezone, timedelta

JAKARTA_TZ = timezone(timedelta(hours=7))

def get_jakarta_now():
    """Returns current naive datetime in Asia/Jakarta timezone (WIB, UTC+7)"""
    return datetime.now(JAKARTA_TZ).replace(tzinfo=None)

def get_jakarta_date():
    """Returns current date in Asia/Jakarta timezone (WIB, UTC+7)"""
    return datetime.now(JAKARTA_TZ).date()

class Settings(BaseSettings):
    WHATSAPP_TOKEN: str = "your_whatsapp_cloud_api_token"
    PHONE_NUMBER_ID: str = "1255768767610034"
    WEBHOOK_VERIFY_TOKEN: str = "your_webhook_verify_token"
    DATABASE_URL: str = "sqlite:///./whatsapp.db"

    class Config:
        env_file = ".env"

settings = Settings()

print("\n--- CEK KONFIGURASI ---")
print(f"Bentuk Token: {settings.WHATSAPP_TOKEN[:15]}... (panjang: {len(settings.WHATSAPP_TOKEN)})")

if '"' in settings.WHATSAPP_TOKEN:
    print("STATUS: [X] ADA TANDA KUTIP (Hapus dari .env)")
else:
    print("STATUS: [OK] AMAN, TIDAK ADA TANDA KUTIP")
    
print("-----------------------\n")