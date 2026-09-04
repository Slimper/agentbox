from datetime import UTC, datetime, timedelta

from agentbox.domain.threading import ThreadCandidate, resolve_thread

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def cand(tid, subj, parts, age_days=0):
    return ThreadCandidate(tid, subj, frozenset(parts), NOW - timedelta(days=age_days))


def test_in_reply_to_wins():
    tid = resolve_thread(
        in_reply_to="<a@x>", references=["<b@x>"],
        by_message_id={"<a@x>": "thr_A", "<b@x>": "thr_B"},
        subject_normalized="anything", participants={"v@s.ru"},
        subject_candidates=[cand("thr_C", "anything", ["v@s.ru"])], now=NOW,
    )
    assert tid == "thr_A"


def test_references_checked_newest_first():
    tid = resolve_thread(
        in_reply_to=None, references=["<old@x>", "<new@x>"],
        by_message_id={"<old@x>": "thr_OLD", "<new@x>": "thr_NEW"},
        subject_normalized="", participants=set(), subject_candidates=[], now=NOW,
    )
    assert tid == "thr_NEW"


def test_subject_and_participant_fallback_within_30_days():
    cands = [cand("thr_1", "quote", ["v@s.ru"], age_days=40), cand("thr_2", "quote", ["v@s.ru"], age_days=3)]
    tid = resolve_thread(
        in_reply_to=None, references=[], by_message_id={},
        subject_normalized="quote", participants={"v@s.ru"}, subject_candidates=cands, now=NOW,
    )
    assert tid == "thr_2"


def test_no_match_returns_none():
    cands = [cand("thr_1", "quote", ["other@s.ru"])]
    assert resolve_thread(
        in_reply_to=None, references=[], by_message_id={},
        subject_normalized="quote", participants={"v@s.ru"}, subject_candidates=cands, now=NOW,
    ) is None
    assert resolve_thread(
        in_reply_to=None, references=[], by_message_id={},
        subject_normalized="", participants={"v@s.ru"}, subject_candidates=cands, now=NOW,
    ) is None
