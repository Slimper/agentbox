from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentbox.api.errors import not_found
from agentbox.db.models import Message, Thread
from agentbox.domain.ids import new_id
from agentbox.domain.subject import normalize_subject
from agentbox.domain.threading import SUBJECT_MATCH_WINDOW, ThreadCandidate, resolve_thread


def participants_of(addresses: list[dict], exclude: str) -> list[str]:
    ex = exclude.lower()
    return sorted({(a.get("email") or "").lower() for a in addresses if a.get("email") and a["email"].lower() != ex})


def thread_to_dict(t: Thread) -> dict:
    return {"id": t.id, "inbox_id": t.inbox_id, "subject": t.subject, "participants": t.participants,
            "message_count": t.message_count, "last_message_at": t.last_message_at.isoformat(),
            "metadata": t.metadata_, "created_at": t.created_at.isoformat()}


async def create_thread(session: AsyncSession, *, organization_id: str, inbox_id: str, subject: str,
                        participants: list[str], at: datetime) -> Thread:
    t = Thread(id=new_id("thr"), organization_id=organization_id, inbox_id=inbox_id, subject=subject,
               subject_normalized=normalize_subject(subject), participants=participants, last_message_at=at,
               message_count=0)
    session.add(t)
    await session.flush()
    return t


def touch_thread(thread: Thread, participants: list[str], at: datetime) -> None:
    thread.participants = sorted(set(thread.participants) | set(participants))
    thread.message_count = (thread.message_count or 0) + 1
    if thread.last_message_at is None or at > thread.last_message_at:
        thread.last_message_at = at


async def find_thread_for_inbound(
    session: AsyncSession, *, organization_id: str, inbox_id: str, in_reply_to: str | None, references: list[str],
    subject_normalized: str, participants: list[str], now: datetime,
) -> Thread | None:
    ids = ([in_reply_to] if in_reply_to else []) + list(references)
    by_message_id: dict[str, str] = {}
    if ids:
        rows = await session.execute(
            select(Message.internet_message_id, Message.thread_id).where(
                Message.organization_id == organization_id, Message.inbox_id == inbox_id,
                Message.internet_message_id.in_(ids))
        )
        by_message_id = {mid: tid for mid, tid in rows}
    candidates: list[ThreadCandidate] = []
    if subject_normalized:
        rows = await session.scalars(
            select(Thread).where(Thread.organization_id == organization_id, Thread.inbox_id == inbox_id,
                                 Thread.subject_normalized == subject_normalized,
                                 Thread.last_message_at >= now - SUBJECT_MATCH_WINDOW - timedelta(days=1))
        )
        candidates = [ThreadCandidate(t.id, t.subject_normalized, frozenset(t.participants), t.last_message_at)
                      for t in rows]
    tid = resolve_thread(in_reply_to=in_reply_to, references=references, by_message_id=by_message_id,
                         subject_normalized=subject_normalized, participants=set(participants),
                         subject_candidates=candidates, now=now)
    return await session.get(Thread, tid) if tid else None


async def get_thread(session: AsyncSession, organization_id: str, thread_id: str) -> Thread:
    t = await session.scalar(select(Thread).where(Thread.id == thread_id, Thread.organization_id == organization_id))
    if t is None:
        raise not_found("thread", thread_id)
    return t
