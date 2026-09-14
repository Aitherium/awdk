"""Database models and Pydantic schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, Float, Integer, String, Text, JSON
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class DocumentRecord(Base):
    __tablename__ = "documents"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String, default="default", index=True, nullable=False)
    filename = Column(String, nullable=False)
    doc_type = Column(String, nullable=False)
    source_url = Column(String, nullable=True)
    user_id = Column(String, default="anonymous")
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    chunk_count = Column(Integer, default=0)
    metadata_json = Column(JSON, default=dict)


class FeedbackRecord(Base):
    __tablename__ = "feedback"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String, default="default", index=True, nullable=False)
    message_id = Column(String)
    user_id = Column(String, default="anonymous")
    rating = Column(Integer)
    correction = Column(Text)
    context = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class ConversationRecord(Base):
    __tablename__ = "conversations"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String, default="default", index=True, nullable=False)
    conversation_id = Column(String, nullable=False, index=True)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    user_id = Column(String, default="anonymous")
    title = Column(String)
    visibility = Column(String, default="shared")
    created_at = Column(DateTime, default=datetime.utcnow)


class ActivityRecord(Base):
    __tablename__ = "activity"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String, default="default", index=True, nullable=False)
    user_id = Column(String, nullable=False)
    user_name = Column(String, default="")
    action = Column(String, nullable=False)
    subject = Column(String, default="")
    detail = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)


# Pydantic schemas

class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None


class SourceRef(BaseModel):
    filename: str
    score: float = 0.0


class LLMUsage(BaseModel):
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    is_fallback: bool = False


class ChatResponse(BaseModel):
    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    response: str
    sources: list[SourceRef] = []
    conversation_id: str
    usage: LLMUsage | None = None


class DocumentUploadResponse(BaseModel):
    doc_id: str
    filename: str
    doc_type: str
    chunks_created: int


class FeedbackRequest(BaseModel):
    message_id: str
    rating: int = Field(ge=1, le=5)
    correction: Optional[str] = None
    context: Optional[str] = None
