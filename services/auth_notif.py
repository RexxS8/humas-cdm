import logging
from core.config import settings, get_jakarta_now
from services.whatsapp import send_text_message

logger = logging.getLogger(__name__)

async def send_2fa_alert_and_otp(
    to_phone: str,
    otp_code: str,
    ip_address: str,
    location_str: str,
    device_str: str,
    username: str = "Admin"
) -> bool:
    """
    Sends 2FA OTP Code and Real-time Security Alert via WhatsApp.
    """
    now_str = get_jakarta_now().strftime("%d %B %Y, %H:%M:%S WIB")
    target_wa = to_phone if to_phone else settings.ADMIN_WA_NUMBER
    
    # Format phone number if needed (strip + or space)
    target_wa = target_wa.replace("+", "").replace(" ", "").replace("-", "")
    if target_wa.startswith("0"):
        target_wa = "62" + target_wa[1:]

    alert_message = (
        f"🚨 *ALERT KEAMANAN WA-CDM* 🚨\n\n"
        f"Ada percobaan login ke Dashboard WA-CDM!\n"
        f"-----------------------------------\n"
        f"👤 *Username* : {username}\n"
        f"🕒 *Waktu*    : {now_str}\n"
        f"🌐 *IP Address*: {ip_address}\n"
        f"📍 *Lokasi*   : {location_str}\n"
        f"💻 *Perangkat*: {device_str}\n\n"
        f"🔑 *KODE OTP 2FA ANDA*: *{otp_code}*\n"
        f"_(Berlaku selama {settings.OTP_EXPIRE_MINUTES} menit. JANGAN BAGIKAN KODE INI SIAPAPUN!)_\n\n"
        f"-----------------------------------\n"
        f"📱 *KONTROL SESI REMOTE VIA WA*:\n"
        f"• Balas */sessions* untuk cek perangkat & user yang sedang login.\n"
        f"• Balas */logout <ID>* (misal: `/logout 2`) untuk mencabut sesi tertentu.\n"
        f"• Balas */logout_all* untuk mencabut SEMUA sesi aktif sekaligus."
    )


    logger.info(f"Sending 2FA OTP ({otp_code}) to {target_wa} via WhatsApp...")
    try:
        success, resp = await send_text_message(target_wa, alert_message)
        if success:
            logger.info(f"Successfully sent 2FA Security Alert to {target_wa}")
            return True
        else:
            logger.error(f"Failed to send 2FA Security Alert to {target_wa}: {resp}")
            return False
    except Exception as e:
        logger.error(f"Exception sending 2FA alert: {e}")
        return False
