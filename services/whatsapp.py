import httpx
import uuid
import mimetypes
import os
from core.config import settings
import logging

logger = logging.getLogger(__name__)

MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "video/mp4": ".mp4"
}

async def download_meta_media(media_id: str) -> str:
    """
    Downloads a media file from Meta Graph API using media_id.
    Saves file into static/uploads/<uuid>.<ext> and returns relative URL path.
    """
    if settings.WHATSAPP_TOKEN == "your_whatsapp_cloud_api_token":
        return "/static/uploads/placeholder.jpg"

    meta_url = f"https://graph.facebook.com/v18.0/{media_id}"
    headers = {
        "Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"
    }

    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            # Step 1: Request media info to get download URL
            res = await client.get(meta_url, headers=headers)
            res.raise_for_status()
            data = res.json()
            
            download_url = data.get("url")
            mime_type = data.get("mime_type", "")

            if not download_url:
                raise ValueError("No download URL found in Meta media response")

            # Step 2: Download raw binary data from Meta URL
            media_res = await client.get(download_url, headers=headers)
            media_res.raise_for_status()

            # Determine extension
            ext = MIME_EXTENSIONS.get(mime_type)
            if not ext:
                ext = mimetypes.guess_extension(mime_type) or ".bin"

            os.makedirs("static/uploads", exist_ok=True)
            filename = f"{uuid.uuid4().hex}{ext}"
            file_path = os.path.join("static", "uploads", filename)

            with open(file_path, "wb") as f:
                f.write(media_res.content)

            return f"/static/uploads/{filename}"

        except Exception as e:
            logger.error(f"Error downloading media {media_id} from Meta: {e}")
            raise e

async def send_template_message(to_phone: str, contact_name: str = "Umat", template_name: str = "donor_darah", language_code: str = "id", header_image_url: str = None):
    if settings.WHATSAPP_TOKEN == "your_whatsapp_cloud_api_token":
        import asyncio
        await asyncio.sleep(0.3)
        return True, {"message": f"Mock success for template {template_name}"}
        
    url = f"https://graph.facebook.com/v25.0/{settings.PHONE_NUMBER_ID}/messages"
    
    headers = {
        "Authorization": f"Bearer {settings.WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {
                "code": language_code
            }
        }
    }
    
    img_url = header_image_url
    if not img_url and template_name == "donor_darah":
        img_url = "https://i.ibb.co.com/9HMSFKby/Donor-Darah.jpg"
        
    if img_url:
        payload["template"]["components"] = [
            {
                "type": "header",
                "parameters": [
                    {
                        "type": "image",
                        "image": {
                            "link": img_url 
                        }
                    }
                ]
            }
        ]
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return True, response.json()
        except httpx.HTTPError as e:
            logger.error(f"Error sending message to {to_phone}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Detail dari Meta: {e.response.text}")
            return False, str(e)


async def send_text_message(to_phone: str, text: str):
    if settings.WHATSAPP_TOKEN == "your_whatsapp_cloud_api_token":
        import asyncio
        await asyncio.sleep(0.3)
        return True, {"message": "Mock success for testing text reply"}

    url = f"https://graph.facebook.com/v25.0/{settings.PHONE_NUMBER_ID}/messages"
    
    headers = {
        "Authorization": f"Bearer {settings.WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {
            "body": text
        }
    }
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return True, response.json()
        except httpx.HTTPError as e:
            print("\n" + "="*50)
            print("🚨 PESAN PENOLAKAN ASLI DARI META 🚨")
            if hasattr(e, 'response') and e.response is not None:
                print(e.response.text)
            else:
                print("Tidak ada detail response text.")
            print("="*50 + "\n")
            
            logger.error(f"Error sending text to {to_phone}: {e}")
            return False, str(e)

async def send_media_message(to_phone: str, media_url: str, media_type: str = "image", caption: str = None):
    """
    Sends an image or document message via WhatsApp Cloud API.
    """
    if settings.WHATSAPP_TOKEN == "your_whatsapp_cloud_api_token":
        import asyncio
        await asyncio.sleep(0.3)
        return True, {"message": "Mock success for testing media message"}

    url = f"https://graph.facebook.com/v25.0/{settings.PHONE_NUMBER_ID}/messages"
    
    headers = {
        "Authorization": f"Bearer {settings.WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }

    # Standardize media type (image, document, sticker, etc)
    msg_type = media_type if media_type in ["image", "document", "sticker", "audio", "video"] else "image"
    if msg_type == "sticker":
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_phone,
            "type": "sticker",
            "sticker": {
                "link": media_url
            }
        }
    else:
        media_obj = {"link": media_url}
        if caption:
            media_obj["caption"] = caption
            
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_phone,
            "type": msg_type,
            msg_type: media_obj
        }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return True, response.json()
        except httpx.HTTPError as e:
            print("\n" + "="*50)
            print("🚨 PESAN PENOLAKAN MEDIA DARI META 🚨")
            if hasattr(e, 'response') and e.response is not None:
                print(e.response.text)
            else:
                print("Tidak ada detail response text.")
            print("="*50 + "\n")
            
            logger.error(f"Error sending media to {to_phone}: {e}")
            return False, str(e)