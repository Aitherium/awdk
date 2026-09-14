"""Feedback and preference endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import UserContext, get_user_context
from ..database import get_db
from ..models import FeedbackRecord, FeedbackRequest

router = APIRouter(prefix="/api/feedback", tags=["feedback"])


@router.post("")
async def submit_feedback(
    req: FeedbackRequest,
    user: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    record = FeedbackRecord(
        message_id=req.message_id,
        rating=req.rating,
        correction=req.correction,
        context=req.context,
        user_id=user.user_id,
        tenant_id=user.tenant_id,
    )
    db.add(record)
    await db.commit()
    return {"status": "ok", "id": record.id}


@router.get("")
async def list_feedback(
    user: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(FeedbackRecord)
        .where(FeedbackRecord.tenant_id == user.tenant_id)
        .order_by(FeedbackRecord.created_at.desc())
        .limit(50)
    )
    return [
        {
            "id": f.id,
            "message_id": f.message_id,
            "rating": f.rating,
            "correction": f.correction,
            "created_at": f.created_at.isoformat() if f.created_at else None,
        }
        for f in result.scalars().all()
    ]
