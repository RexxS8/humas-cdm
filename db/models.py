from sqlalchemy import Column, Integer, String, Date, Boolean, DateTime
from db.database import Base
from core.config import get_jakarta_now, get_jakarta_date
import datetime

class DailyLimitTracker(Base):
    __tablename__ = "daily_limit_tracker"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, unique=True, index=True, default=get_jakarta_date)
    sent_count = Column(Integer, default=0)

class MessageLog(Base):
    __tablename__ = "message_log"

    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String, index=True)
    status = Column(String, default="sent")
    created_at = Column(DateTime, default=get_jakarta_now)

class Contact(Base):
    __tablename__ = "contacts"
    
    phone_number = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=True)
    label = Column(String, nullable=True) # e.g. "Donatur", "Pengurus", "Umat Baru"
    unread_count = Column(Integer, default=0)
    last_message = Column(String, nullable=True)
    last_message_at = Column(DateTime, default=get_jakarta_now)
    last_blasted_at = Column(DateTime, nullable=True)



class ChatMessage(Base):
    __tablename__ = "chat_messages"
    
    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String, index=True)
    direction = Column(String) # "inbound" or "outbound"
    text = Column(String, nullable=True)
    msg_type = Column(String, default="text") # "text", "image", "sticker", "document"
    media_url = Column(String, nullable=True)
    status = Column(String, default="sent") # "sent", "delivered", "read", "failed"
    error_detail = Column(String, nullable=True)
    timestamp = Column(DateTime, default=get_jakarta_now)

class DeletedLabel(Base):
    __tablename__ = "deleted_labels"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)

class GlobalLabel(Base):
    __tablename__ = "global_labels"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    color = Column(String, nullable=True)




