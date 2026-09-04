from dataclasses import dataclass
from datetime import datetime, timedelta

SUBJECT_MATCH_WINDOW = timedelta(days=30)


@dataclass(frozen=True)
class ThreadCandidate:
    thread_id: str
    subject_normalized: str
    participants: frozenset[str]
    last_message_at: datetime


def resolve_thread(
    *,
    in_reply_to: str | None,
    references: list[str],
    by_message_id: dict[str, str],
    subject_normalized: str,
    participants: set[str],
    subject_candidates: list[ThreadCandidate],
    now: datetime,
) -> str | None:
    """Spec §8: In-Reply-To, References (newest first), subject+participants within 30 days, else None."""
    ordered: list[str] = []
    if in_reply_to:
        ordered.append(in_reply_to)
    ordered.extend(reversed(references))
    for mid in ordered:
        if mid in by_message_id:
            return by_message_id[mid]
    if not subject_normalized:
        return None
    for c in sorted(subject_candidates, key=lambda c: c.last_message_at, reverse=True):
        if c.subject_normalized != subject_normalized:
            continue
        if now - c.last_message_at > SUBJECT_MATCH_WINDOW:
            continue
        if c.participants & participants:
            return c.thread_id
    return None
