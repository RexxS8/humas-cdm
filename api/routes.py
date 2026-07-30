from fastapi import APIRouter, BackgroundTasks, Depends, Request, HTTPException, UploadFile, File, Form, Response
from sqlalchemy.orm import Session
from core.config import settings, get_jakarta_now, get_jakarta_date
from db.database import get_db
from db.models import DailyLimitTracker, MessageLog, Contact, ChatMessage, DeletedLabel, GlobalLabel, UserSession, User
from services.parser import parse_umat_csv, parse_contacts_csv_content, clean_phone_number
from services.whatsapp import send_template_message, send_text_message, send_media_message, download_meta_media
from api.websocket import manager
from api.auth import get_current_user
from pydantic import BaseModel
import datetime
import logging
import re
import uuid
import os


from typing import Optional, List

logger = logging.getLogger(__name__)
router = APIRouter()

DAILY_LIMIT = 250

class SendChatRequest(BaseModel):
    phone_number: str
    message: str

class SendMediaRequest(BaseModel):
    phone_number: str
    media_url: str
    media_type: str = "image"
    caption: Optional[str] = None

class BlastRequest(BaseModel):
    template_name: str = "donor_darah"
    language_code: str = "id"
    header_image_url: Optional[str] = None
    target_label: Optional[str] = None
    selected_phones: Optional[List[str]] = None
    limit_count: int = 250

class UpdateLabelRequest(BaseModel):
    phone_number: str
    label: Optional[str] = None

class AddContactRequest(BaseModel):
    phone_number: str
    name: Optional[str] = None
    label: Optional[str] = None

class RenameLabelRequest(BaseModel):
    old_label: str
    new_label: str

class DeleteLabelRequest(BaseModel):
    label_name: str

class AddGlobalLabelRequest(BaseModel):
    label_name: str
    color: Optional[str] = None

def get_preview_text(msg_type: str, text: str = "") -> str:
    if msg_type == 'image':
        return f"📷 {text}" if text else "📷 Gambar"
    elif msg_type == 'sticker':
        return "[Sticker]"
    elif msg_type == 'document':
        return f"📄 {text}" if text else "📄 Dokumen"
    elif msg_type in ['audio', 'video']:
        return f"🎥 {text}" if text else f"🎥 {msg_type.capitalize()}"
    return text or ""

async def process_blast(db: Session, contacts: list, template_name: str = "donor_darah", language_code: str = "id", header_image_url: str = None):
    today = get_jakarta_date()
    tracker = db.query(DailyLimitTracker).filter_by(date=today).first()
    
    if not tracker:
        tracker = DailyLimitTracker(date=today, sent_count=0)
        db.add(tracker)
        db.commit()
        db.refresh(tracker)
        
    for contact in contacts:
        success, response = await send_template_message(
            contact['phone'], 
            contact.get('name', 'Umat'),
            template_name=template_name,
            language_code=language_code,
            header_image_url=header_image_url
        )
        
        status = "sent" if success else "failed"
        log = MessageLog(phone_number=contact['phone'], status=status)
        db.add(log)
        
        # Tambahkan juga ke ChatMessage agar muncul di UI
        if success:
            now = get_jakarta_now()
            blast_text = f"[BLAST] Template '{template_name}' terkirim"
            chat_msg = ChatMessage(phone_number=contact['phone'], direction="outbound", text=blast_text, msg_type="text", status="sent", timestamp=now)
            db.add(chat_msg)
            
            # Update Contact
            c = db.query(Contact).filter_by(phone_number=contact['phone']).first()
            if not c:
                c = Contact(phone_number=contact['phone'], name=contact.get('name'))
                db.add(c)
            c.last_message_at = now
            c.last_message = blast_text
            c.last_blasted_at = now
            
            tracker.sent_count += 1
            
        db.commit()

@router.post("/api/blast")
async def trigger_blast(
    background_tasks: BackgroundTasks,
    req: BlastRequest = BlastRequest(),
    db: Session = Depends(get_db)
):
    today = get_jakarta_date()
    tracker = db.query(DailyLimitTracker).filter_by(date=today).first()
    sent_today = tracker.sent_count if tracker else 0
    
    # Target contacts selection logic
    target_contacts = []
    if req.selected_phones and len(req.selected_phones) > 0:
        contacts_query = db.query(Contact).filter(Contact.phone_number.in_(req.selected_phones)).all()
        phone_map = {c.phone_number: c.name or "Umat" for c in contacts_query}
        for p in req.selected_phones:
            target_contacts.append({"phone": p, "name": phone_map.get(p, "Umat")})
    elif req.target_label and req.target_label.upper() != "ALL":
        target_norm = req.target_label.strip().lower()
        all_contacts = db.query(Contact).filter(Contact.label.isnot(None)).all()
        target_contacts = []
        for c in all_contacts:
            if c.label:
                parts = [p.strip().lower() for p in c.label.split(',') if p.strip()]
                if target_norm in parts:
                    target_contacts.append({"phone": c.phone_number, "name": c.name or "Umat"})
    else:
        contacts_query = db.query(Contact).all()
        target_contacts = [{"phone": c.phone_number, "name": c.name or "Umat"} for c in contacts_query]
    
    # Fallback to umat.csv if DB contacts empty
    if not target_contacts:
        target_contacts = parse_umat_csv()
        
    if not target_contacts:
        return {"status": "error", "message": "Tidak ada kontak sasaran yang ditemukan untuk di-blast."}
    
    background_tasks.add_task(process_blast, db, target_contacts, req.template_name, req.language_code, req.header_image_url)
    
    return {
        "status": "success", 
        "message": f"Blast '{req.template_name}' berhasil dijadwalkan untuk {len(target_contacts)} kontak.", 
        "queued": len(target_contacts),
        "sent_today": sent_today
    }

@router.get("/api/chat/contacts")
async def get_contacts(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    contacts = db.query(Contact).order_by(Contact.last_message_at.desc()).all()
    
    # Check existing ChatMessages for blast fallback timestamp
    blast_msgs = db.query(
        ChatMessage.phone_number,
        ChatMessage.timestamp
    ).filter(ChatMessage.text.like("[BLAST]%")).all()
    
    blast_map = {}
    for b in blast_msgs:
        p, ts = b[0], b[1]
        if p not in blast_map or (ts and (blast_map[p] is None or ts > blast_map[p])):
            blast_map[p] = ts

    res = []
    for c in contacts:
        blast_time = getattr(c, "last_blasted_at", None) or blast_map.get(c.phone_number)
        res.append({
            "phone_number": c.phone_number, 
            "name": c.name, 
            "label": getattr(c, "label", None),
            "unread_count": getattr(c, "unread_count", 0) or 0,
            "last_message": getattr(c, "last_message", None),
            "last_message_at": c.last_message_at,
            "last_blasted_at": blast_time
        })
    return res

@router.get("/api/chat/labels")
async def get_labels(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):

    db_labels = db.query(Contact.label).filter(Contact.label.isnot(None), Contact.label != "").all()
    global_labels = db.query(GlobalLabel.name).all()
    deleted_records = db.query(DeletedLabel.name).all()
    deleted_names = set([d[0].strip().lower() for d in deleted_records if d[0]])

    unique_labels = set()
    for row in db_labels:
        if row[0]:
            parts = [p.strip() for p in str(row[0]).split(',') if p.strip()]
            for p in parts:
                if p.lower() not in deleted_names:
                    unique_labels.add(p)
                    
    for g in global_labels:
        if g[0] and g[0].strip().lower() not in deleted_names:
            unique_labels.add(g[0].strip())
                
    defaults = ["Donor Darah", "Donatur", "Pengurus", "Umat Baru", "Donatur Pattidana"]
    for d in defaults:
        if d.lower() not in deleted_names:
            unique_labels.add(d)
            
    return sorted(list(unique_labels))

@router.post("/api/labels/add")
async def add_global_label(req: AddGlobalLabelRequest, db: Session = Depends(get_db)):
    name = req.label_name.strip() if req.label_name else ""
    if not name:
        raise HTTPException(status_code=400, detail="Nama label tidak boleh kosong.")
        
    db.query(DeletedLabel).filter(DeletedLabel.name.ilike(name)).delete(synchronize_session=False)
    
    existing = db.query(GlobalLabel).filter(GlobalLabel.name.ilike(name)).first()
    if not existing:
        lbl = GlobalLabel(name=name, color=req.color)
        db.add(lbl)
    else:
        if req.color:
            existing.color = req.color
            
    db.commit()
    return {"status": "success", "message": f"Label '{name}' berhasil ditambahkan ke Kelola Label."}

@router.post("/api/labels/rename")
async def rename_label(req: RenameLabelRequest, db: Session = Depends(get_db)):
    old_target = req.old_label.strip() if req.old_label else ""
    new_target = req.new_label.strip() if req.new_label else ""
    
    if not old_target or not new_target:
        raise HTTPException(status_code=400, detail="Label lama dan baru tidak boleh kosong.")
        
    contacts = db.query(Contact).filter(Contact.label.isnot(None), Contact.label != "").all()
    count = 0
    for c in contacts:
        parts = [p.strip() for p in c.label.split(',') if p.strip()]
        new_parts = []
        changed = False
        for p in parts:
            if p.lower() == old_target.lower():
                new_parts.append(new_target)
                changed = True
            else:
                new_parts.append(p)
        if changed:
            c.label = ", ".join(new_parts)
            count += 1
            
    # Rename in GlobalLabel if present
    g_existing = db.query(GlobalLabel).filter(GlobalLabel.name.ilike(old_target)).first()
    if g_existing:
        g_existing.name = new_target
        
    db.query(DeletedLabel).filter(DeletedLabel.name.ilike(old_target)).delete(synchronize_session=False)
    db.query(DeletedLabel).filter(DeletedLabel.name.ilike(new_target)).delete(synchronize_session=False)
    
    db.commit()
    return {"status": "success", "message": f"Label '{old_target}' berhasil diubah menjadi '{new_target}'.", "updated_contacts": count}

@router.post("/api/labels/delete")
async def delete_label_global(req: DeleteLabelRequest, db: Session = Depends(get_db)):
    target = req.label_name.strip() if req.label_name else ""
    if not target:
        raise HTTPException(status_code=400, detail="Nama label tidak boleh kosong.")
        
    contacts = db.query(Contact).filter(Contact.label.isnot(None), Contact.label != "").all()
    count = 0
    for c in contacts:
        parts = [p.strip() for p in c.label.split(',') if p.strip()]
        new_parts = [p for p in parts if p.lower() != target.lower()]
        if len(new_parts) != len(parts):
            c.label = ", ".join(new_parts) if new_parts else None
            count += 1
            
    db.query(GlobalLabel).filter(GlobalLabel.name.ilike(target)).delete(synchronize_session=False)
            
    existing = db.query(DeletedLabel).filter(DeletedLabel.name.ilike(target)).first()
    if not existing:
        del_entry = DeletedLabel(name=target)
        db.add(del_entry)
        
    db.commit()
    return {"status": "success", "message": f"Label '{target}' berhasil dihapus secara permanen.", "updated_contacts": count}

@router.get("/api/contacts/template-csv")
async def download_template_csv():
    csv_data = "name,phone,label\nNama Contoh 1,6280000000001,Donor Darah\nNama Contoh 2,6280000000002,Donatur Pattidana\nNama Contoh 3,6280000000003,Pengurus\n"
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="template_kontak.csv"'}
    )

@router.post("/api/contacts/import-csv")
async def import_contacts_csv(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="File harus berformat .csv")
    
    contents = await file.read()
    try:
        csv_text = contents.decode("utf-8")
    except UnicodeDecodeError:
        csv_text = contents.decode("latin-1")

    parsed_contacts = parse_contacts_csv_content(csv_text)
    if not parsed_contacts:
        return {"status": "error", "message": "Tidak ada kontak valid yang ditemukan dalam file CSV."}
    
    count_added = 0
    count_updated = 0

    for item in parsed_contacts:
        phone = item["phone"]
        name = item.get("name")
        label = item.get("label")

        c = db.query(Contact).filter_by(phone_number=phone).first()
        if not c:
            c = Contact(phone_number=phone, name=name, label=label)
            db.add(c)
            count_added += 1
        else:
            if name and name.strip():
                c.name = name.strip()
            if label and label.strip():
                existing_labels = [p.strip() for p in (c.label or "").split(",") if p.strip()]
                new_labels = [p.strip() for p in label.split(",") if p.strip()]
                for n_lbl in new_labels:
                    if not any(n_lbl.lower() == e_lbl.lower() for e_lbl in existing_labels):
                        existing_labels.append(n_lbl)
                c.label = ", ".join(existing_labels)
            count_updated += 1

    db.commit()
    return {
        "status": "success",
        "message": f"Berhasil memproses CSV. {count_added} kontak baru ditambahkan, {count_updated} kontak diperbarui (label ditumpuk secara otomatis).",
        "added": count_added,
        "updated": count_updated
    }

@router.post("/api/contacts/add")
async def add_manual_contact(req: AddContactRequest, db: Session = Depends(get_db)):
    cleaned = clean_phone_number(req.phone_number)
    if not cleaned or len(cleaned) < 10:
        raise HTTPException(
            status_code=400,
            detail="Nomor telepon WhatsApp tidak valid. Masukkan nomor yang benar (contoh: 08569873731 atau 628569873731)."
        )
    
    c = db.query(Contact).filter_by(phone_number=cleaned).first()
    name_val = req.name.strip() if req.name and req.name.strip() else cleaned
    label_val = req.label.strip() if req.label and req.label.strip() else None

    if not c:
        c = Contact(phone_number=cleaned, name=name_val, label=label_val)
        db.add(c)
    else:
        if req.name and req.name.strip():
            c.name = req.name.strip()
        if label_val:
            existing_labels = [p.strip() for p in (c.label or "").split(",") if p.strip()]
            new_labels = [p.strip() for p in label_val.split(",") if p.strip()]
            for n_lbl in new_labels:
                if not any(n_lbl.lower() == e_lbl.lower() for e_lbl in existing_labels):
                    existing_labels.append(n_lbl)
            c.label = ", ".join(existing_labels)
    
    db.commit()
    db.refresh(c)
    logger.info(f"Manual contact added/updated: {cleaned} - {c.name} (Label: {c.label})")
    return {
        "status": "success",
        "message": f"Kontak '{c.name}' (+{cleaned}) berhasil disimpan.",
        "phone_number": cleaned,
        "name": c.name,
        "label": c.label
    }

@router.delete("/api/contacts/{phone_number}")
async def delete_contact(phone_number: str, db: Session = Depends(get_db)):
    c = db.query(Contact).filter_by(phone_number=phone_number).first()
    if not c:
        raise HTTPException(status_code=404, detail="Kontak tidak ditemukan.")
    db.delete(c)
    db.commit()
    return {"status": "success", "message": f"Kontak {phone_number} telah dihapus."}

@router.post("/api/chat/read/{phone_number}")
async def mark_chat_read(phone_number: str, db: Session = Depends(get_db)):
    contact = db.query(Contact).filter_by(phone_number=phone_number).first()
    if contact:
        contact.unread_count = 0
        db.commit()
    return {"status": "success", "phone_number": phone_number, "unread_count": 0}

@router.post("/api/chat/contacts/label")
async def update_contact_label(req: UpdateLabelRequest, db: Session = Depends(get_db)):
    contact = db.query(Contact).filter_by(phone_number=req.phone_number).first()
    if not contact:
        contact = Contact(phone_number=req.phone_number)
        db.add(contact)
    contact.label = req.label
    db.commit()
    return {"status": "success", "message": "Label updated", "phone_number": req.phone_number, "label": req.label}


@router.get("/api/chat/stats")
async def get_chat_stats(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    today = get_jakarta_date()
    tracker = db.query(DailyLimitTracker).filter_by(date=today).first()
    sent_today = tracker.sent_count if tracker else 0

    sent_count = db.query(ChatMessage).filter(ChatMessage.status == "sent", ChatMessage.direction == "outbound").count()
    delivered_count = db.query(ChatMessage).filter(ChatMessage.status == "delivered", ChatMessage.direction == "outbound").count()
    read_count = db.query(ChatMessage).filter(ChatMessage.status == "read", ChatMessage.direction == "outbound").count()
    failed_count = db.query(ChatMessage).filter(ChatMessage.status == "failed", ChatMessage.direction == "outbound").count()
    return {
        "sent": sent_count,
        "delivered": delivered_count,
        "read": read_count,
        "failed": failed_count,
        "sent_today": sent_today
    }


@router.get("/api/chat/history/{phone_number}")
async def get_chat_history(phone_number: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):

    messages = db.query(ChatMessage).filter(ChatMessage.phone_number == phone_number).order_by(ChatMessage.timestamp.asc()).all()
    return [{
        "id": m.id, 
        "direction": m.direction, 
        "text": m.text, 
        "msg_type": getattr(m, "msg_type", "text") or "text",
        "media_url": getattr(m, "media_url", None),
        "status": m.status or "sent", 
        "error": getattr(m, "error_detail", None),
        "timestamp": m.timestamp
    } for m in messages]

@router.post("/api/chat/send")
async def send_chat(req: SendChatRequest, db: Session = Depends(get_db)):
    success, response = await send_text_message(req.phone_number, req.message)
    if success:
        now = get_jakarta_now()
        # Save to DB
        msg = ChatMessage(phone_number=req.phone_number, direction="outbound", text=req.message, msg_type="text", status="sent", timestamp=now)
        db.add(msg)
        
        # Update contact last_message_at & last_message
        contact = db.query(Contact).filter_by(phone_number=req.phone_number).first()
        if not contact:
            contact = Contact(phone_number=req.phone_number)
            db.add(contact)
        contact.last_message_at = now
        contact.last_message = get_preview_text("text", req.message)
        
        db.commit()
        return {"status": "success", "message": "Message sent"}
    else:
        raise HTTPException(status_code=500, detail="Failed to send message")

@router.post("/api/chat/send-media")
async def send_media(req: SendMediaRequest, request: Request, db: Session = Depends(get_db)):
    # Construct full URL if relative
    full_media_url = req.media_url
    if req.media_url.startswith("/"):
        base_url = str(request.base_url).rstrip("/")
        full_media_url = f"{base_url}{req.media_url}"

    success, response = await send_media_message(req.phone_number, full_media_url, req.media_type, req.caption)
    if success:
        now = get_jakarta_now()
        msg = ChatMessage(
            phone_number=req.phone_number,
            direction="outbound",
            text=req.caption or "",
            msg_type=req.media_type,
            media_url=req.media_url,
            status="sent",
            timestamp=now
        )
        db.add(msg)
        
        contact = db.query(Contact).filter_by(phone_number=req.phone_number).first()
        if not contact:
            contact = Contact(phone_number=req.phone_number)
            db.add(contact)
        contact.last_message_at = now
        contact.last_message = get_preview_text(req.media_type, req.caption)
        
        db.commit()
        return {"status": "success", "message": "Media sent", "media_url": req.media_url}
    else:
        raise HTTPException(status_code=500, detail=f"Failed to send media: {response}")

@router.post("/api/chat/send-media-file")
async def send_media_file(
    request: Request,
    phone_number: str = Form(...),
    media_type: str = Form("image"),
    caption: str = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    os.makedirs("static/uploads", exist_ok=True)
    ext = os.path.splitext(file.filename)[1] or (".jpg" if media_type == "image" else ".bin")
    filename = f"{uuid.uuid4().hex}{ext}"
    local_path = os.path.join("static", "uploads", filename)

    contents = await file.read()
    with open(local_path, "wb") as f:
        f.write(contents)

    relative_url = f"/static/uploads/{filename}"
    base_url = str(request.base_url).rstrip("/")
    full_media_url = f"{base_url}{relative_url}"

    success, response = await send_media_message(phone_number, full_media_url, media_type, caption)
    if success:
        now = get_jakarta_now()
        msg = ChatMessage(
            phone_number=phone_number,
            direction="outbound",
            text=caption or file.filename,
            msg_type=media_type,
            media_url=relative_url,
            status="sent",
            timestamp=now
        )
        db.add(msg)

        contact = db.query(Contact).filter_by(phone_number=phone_number).first()
        if not contact:
            contact = Contact(phone_number=phone_number)
            db.add(contact)
        contact.last_message_at = now
        contact.last_message = get_preview_text(media_type, caption or file.filename)

        db.commit()
        return {"status": "success", "message": "Media sent", "media_url": relative_url}

        db.commit()
        return {"status": "success", "message": "Media sent", "media_url": relative_url}
    else:
        raise HTTPException(status_code=500, detail=f"Failed to send media file: {response}")

@router.get("/webhook/whatsapp")
async def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    
    if mode and token:
        if mode == "subscribe" and token == settings.WEBHOOK_VERIFY_TOKEN:
            return int(challenge)
        else:
            raise HTTPException(status_code=403, detail="Verification failed")
    raise HTTPException(status_code=400, detail="Missing parameters")

@router.post("/webhook/whatsapp")
async def receive_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    
    try:
        for entry in body.get('entry', []):
            for change in entry.get('changes', []):
                value = change.get('value', {})

                # Handle Status Updates (Delivery Receipts)
                if 'statuses' in value:
                    for status_item in value['statuses']:
                        recipient_id = status_item.get('recipient_id')
                        status = status_item.get('status')
                        errors = status_item.get('errors')
                        error_detail = None
                        if errors and isinstance(errors, list) and len(errors) > 0:
                            err_item = errors[0]
                            code = err_item.get('code')
                            title = err_item.get('title') or err_item.get('message') or ''
                            details = err_item.get('error_data', {}).get('details') or ''
                            error_detail = f"[{code}] {title} {details}".strip()
                            logger.error(f"🚨 META DELIVERY STATUS: {status} for {recipient_id}: {error_detail}")
                            print(f"\n🚨 META DELIVERY STATUS: {status} for {recipient_id}: {error_detail}\n")
                        
                        if recipient_id and status:
                            last_msg = db.query(ChatMessage).filter(
                                ChatMessage.phone_number == recipient_id,
                                ChatMessage.direction == "outbound"
                            ).order_by(ChatMessage.timestamp.desc()).first()

                            if last_msg:
                                last_msg.status = status
                                if error_detail:
                                    last_msg.error_detail = error_detail
                                db.commit()

                            await manager.broadcast({
                                "type": "message_status_update",
                                "phone_number": recipient_id,
                                "status": status,
                                "error": error_detail
                            })

                # Handle Inbound Messages
                if 'messages' in value:
                    for message in value['messages']:
                        from_number = message.get('from')
                        msg_type = message.get('type', 'text')
                        
                        text = ""
                        media_url = None

                        if msg_type == 'text':
                            text = message.get('text', {}).get('body', '')
                            cmd_raw = text.strip()
                            cmd_lower = cmd_raw.lower()

                            # 1. Remote Logout All (/logout_all, /logoutall, /kill_sessions, /kickall)
                            if cmd_lower in ['/logout_all', '/logoutall', '/kill_sessions', '/kickall']:
                                db.query(UserSession).update({"is_active": False})
                                db.commit()
                                await manager.broadcast({"type": "session_revoked", "all": True})
                                logger.info(f"🚨 REMOTE KILL-SWITCH TRIGGERED by {from_number}")
                                await send_text_message(
                                    from_number,
                                    "🚨 *REMOTE KILL-SWITCH AKTIF* 🚨\n\nSeluruh sesi login Dashboard WA-CDM dari semua user & perangkat telah dibatalkan. Pengguna harus login ulang."
                                )

                            # 2. Interactive Remote Session Inspection (/sessions, /list_sessions, /status, /cek_sesi)
                            elif cmd_lower in ['/sessions', '/session', '/list_sessions', '/status', '/cek_sesi', '/perangkat']:
                                active_sessions = db.query(UserSession, User)\
                                    .join(User, UserSession.user_id == User.id)\
                                    .filter(UserSession.is_active == True, UserSession.expires_at >= get_jakarta_now())\
                                    .order_by(UserSession.id.desc()).all()

                                if not active_sessions:
                                    reply_msg = "ℹ️ *INFO SESI AKTIF*: Tidak ada sesi login aktif saat ini."
                                else:
                                    lines = ["📱 *DAFTAR SESI AKTIF DASHBOARD WA-CDM* 📱", "-----------------------------------"]
                                    for idx, (sess, usr) in enumerate(active_sessions, 1):
                                        exp_str = sess.expires_at.strftime("%d %b %Y, %H:%M WIB") if sess.expires_at else "-"
                                        role_label = f"({usr.role.upper()})" if usr.role else "(SUBADMIN)"
                                        lines.append(
                                            f"*{idx}. [ID Sesi: {sess.id}]* 👤 User: *{usr.username}* {role_label}\n"
                                            f"   💻 Perangkat: {sess.user_agent or 'Unknown'}\n"
                                            f"   🌐 IP: {sess.ip_address or '-'} ({sess.location or 'Lokal'})\n"
                                            f"   ⏳ Aktif s/d: {exp_str}"
                                        )
                                    lines.append("-----------------------------------")
                                    lines.append("💡 *PETUNJUK KONTROL REMOTE VIA WA*:")
                                    lines.append("• Balas `/logout <ID>` (misal: `/logout 12`) untuk mencabut sesi tertentu.")
                                    lines.append("• Balas `/logout_all` untuk mencabut SEMUA sesi aktif sekaligus.")
                                    reply_msg = "\n".join(lines)

                                await send_text_message(from_number, reply_msg)

                            # 3. Remote Logout Specific Session (/logout <ID> or /kick <ID>)
                            elif cmd_lower.startswith('/logout') or cmd_lower.startswith('/kick') or cmd_lower.startswith('/revoke'):
                                match = re.search(r'\d+', cmd_raw)
                                if match:
                                    target_sess_id = int(match.group())
                                    sess = db.query(UserSession).filter(UserSession.id == target_sess_id, UserSession.is_active == True).first()
                                    if sess:
                                        target_usr = db.query(User).filter(User.id == sess.user_id).first()
                                        usr_name = target_usr.username if target_usr else 'Unknown'
                                        role_name = (target_usr.role or 'subadmin').upper() if target_usr else 'SUBADMIN'
                                        device_name = sess.user_agent or 'Unknown Device'

                                        sess.is_active = False
                                        db.commit()

                                        # Broadcast WebSocket revoke event so the browser redirects immediately
                                        await manager.broadcast({"type": "session_revoked", "session_id": target_sess_id})

                                        logger.info(f"✅ SESSION #{target_sess_id} REVOKED by remote WA command from {from_number}")

                                        await send_text_message(
                                            from_number,
                                            f"✅ *SESI DICABUT (LOGOUT SUCCESS)* ✅\n\n"
                                            f"Sesi ID *#{target_sess_id}*\n"
                                            f"👤 User: *{usr_name}* ({role_name})\n"
                                            f"💻 Perangkat: {device_name}\n\n"
                                            f"Perangkat tersebut telah dinonaktifkan dan otomatis ter-logout dari Dashboard saat ini."
                                        )
                                    else:
                                        await send_text_message(
                                            from_number,
                                            f"❌ *Sesi ID #{target_sess_id}* tidak ditemukan atau sudah tidak aktif lagi.\nBalas `/sessions` untuk mengecek daftar ID sesi yang aktif."
                                        )
                                else:
                                    await send_text_message(
                                        from_number,
                                        "⚠️ Format perintah salah. Gunakan format `/logout <ID>` (contoh: `/logout 12`) atau `/logout_all`."
                                    )


                        elif msg_type in ['image', 'sticker', 'document', 'audio', 'video']:
                            media_data = message.get(msg_type, {})
                            media_id = media_data.get('id')
                            caption = media_data.get('caption') or media_data.get('filename') or ''
                            text = caption
                            
                            if media_id:
                                try:
                                    media_url = await download_meta_media(media_id)
                                except Exception as err:
                                    logger.error(f"Failed to download webhook media {media_id}: {err}")
                        
                        # Find contact profile name if available
                        name = None
                        if 'contacts' in value:
                            for c in value['contacts']:
                                if c.get('wa_id') == from_number:
                                    name = c.get('profile', {}).get('name')
                        
                        now = get_jakarta_now()
                        preview_text = get_preview_text(msg_type, text)

                        # Save to database
                        contact = db.query(Contact).filter_by(phone_number=from_number).first()
                        if not contact:
                            contact = Contact(phone_number=from_number, name=name, unread_count=1, last_message=preview_text, last_message_at=now)
                            db.add(contact)
                        else:
                            contact.last_message_at = now
                            contact.last_message = preview_text
                            contact.unread_count = (contact.unread_count or 0) + 1
                            if name and not contact.name:
                                contact.name = name
                        
                        msg = ChatMessage(
                            phone_number=from_number,
                            direction="inbound",
                            text=text,
                            msg_type=msg_type,
                            media_url=media_url,
                            timestamp=now
                        )
                        db.add(msg)
                        db.commit()
                        
                        # Broadcast to the dashboard
                        await manager.broadcast({
                            "type": "incoming_message",
                            "phone_number": from_number,
                            "name": name or from_number,
                            "text": text,
                            "msg_type": msg_type,
                            "media_url": media_url,
                            "unread_count": contact.unread_count,
                            "last_message": preview_text,
                            "timestamp": now.isoformat()
                        })
    except Exception as e:
        logger.error(f"Error parsing webhook: {e}")
        
    return {"status": "ok"}



