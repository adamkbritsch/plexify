"""Job manager: tracks long-running background operations for the UI."""
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import desc, select

from .db import Job, JobEvent, session_scope

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_json_loads(raw):
    """H6 read-side defense: a row written before H6 was fixed may contain
    truncated/invalid JSON. Return a sentinel dict instead of raising."""
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as e:
        return {"_invalid_json": True, "preview": (raw or "")[:500], "error": str(e)}


def create(kind: str, title: str, *, pair_id: Optional[int] = None, total: int = 0) -> int:
    with session_scope() as s:
        j = Job(kind=kind, title=title, pair_id=pair_id, status="pending",
                progress_total=total, progress_current=0)
        s.add(j)
        s.flush()
        return j.id


def start(job_id: int, step: Optional[str] = None) -> None:
    with session_scope() as s:
        j = s.get(Job, job_id)
        if j:
            j.status = "running"
            if step is not None:
                j.progress_step = step[:255]
    emit(job_id, "started" if step is None else step)


def emit(job_id: int, message: str, level: str = "info") -> None:
    """Append a log event to the job."""
    # Q25: log + mark when we truncate so debugging long messages is possible
    truncated = len(message) > 2000
    payload = message[:2000]
    if truncated:
        log.warning("jobs.emit: truncating message for job %d (was %d chars)", job_id, len(message))
        payload = message[:1996] + "…(…)"
    with session_scope() as s:
        s.add(JobEvent(job_id=job_id, message=payload, level=level))


def progress(job_id: int, current: Optional[int] = None, *,
             total: Optional[int] = None, step: Optional[str] = None,
             message: Optional[str] = None) -> None:
    with session_scope() as s:
        j = s.get(Job, job_id)
        if not j:
            return
        if current is not None:
            j.progress_current = current
        if total is not None:
            j.progress_total = total
        if step is not None:
            j.progress_step = step[:255]
        if message is not None:
            j.message = message[:1000]
    if message:
        emit(job_id, message)


def finish(job_id: int, *, status: str = "ok", result: Optional[Any] = None,
           message: Optional[str] = None) -> None:
    with session_scope() as s:
        j = s.get(Job, job_id)
        if not j:
            return
        j.status = status
        j.finished_at = _utcnow()
        if result is not None:
            # H6: previous code truncated dump() at 8000 chars, producing
            # invalid JSON that crashed jobs.get(). If the full dump fits,
            # use it; otherwise stash a truncated preview as VALID JSON.
            try:
                full = json.dumps(result)
                if len(full) <= 8000:
                    j.result_json = full
                else:
                    j.result_json = json.dumps({
                        "_truncated": True,
                        "_original_len": len(full),
                        "preview": full[:7000],
                    })
            except Exception:
                j.result_json = json.dumps({"_unserializable": str(result)[:7000]})
        if message is not None:
            j.message = message[:1000]
        if j.progress_total > 0 and j.progress_current < j.progress_total and status == "ok":
            j.progress_current = j.progress_total
    emit(job_id, message or status, level="info" if status == "ok" else "error")


def fail(job_id: int, error: str) -> None:
    finish(job_id, status="error", message=error[:1000])


def get(job_id: int, *, last_event_id: int = 0) -> Optional[dict]:
    """Return a snapshot of the job for the UI, plus events newer than `last_event_id`."""
    with session_scope() as s:
        j = s.get(Job, job_id)
        if not j:
            return None
        evs = list(s.scalars(
            select(JobEvent)
            .where(JobEvent.job_id == job_id, JobEvent.id > last_event_id)
            .order_by(JobEvent.id)
            .limit(500)
        ).all())
        return {
            "id": j.id,
            "kind": j.kind,
            "title": j.title,
            "status": j.status,
            "message": j.message,
            "progress_current": j.progress_current,
            "progress_total": j.progress_total,
            "progress_step": j.progress_step,
            "started_at": j.started_at.isoformat() if j.started_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
            "result": _safe_json_loads(j.result_json) if j.result_json else None,
            "pair_id": j.pair_id,
            "events": [
                {"id": e.id, "ts": e.ts.isoformat(), "level": e.level, "message": e.message}
                for e in evs
            ],
        }


def active() -> list[dict]:
    """All jobs currently pending or running."""
    with session_scope() as s:
        rows = list(s.scalars(
            select(Job).where(Job.status.in_(["pending", "running"])).order_by(desc(Job.started_at))
        ).all())
        return [
            {"id": j.id, "kind": j.kind, "title": j.title, "status": j.status,
             "progress_current": j.progress_current, "progress_total": j.progress_total,
             "progress_step": j.progress_step}
            for j in rows
        ]


def recent(limit: int = 25) -> list[dict]:
    with session_scope() as s:
        rows = list(s.scalars(select(Job).order_by(desc(Job.started_at)).limit(limit)).all())
        return [
            {"id": j.id, "kind": j.kind, "title": j.title, "status": j.status,
             "progress_current": j.progress_current, "progress_total": j.progress_total,
             "started_at": j.started_at.isoformat() if j.started_at else None,
             "finished_at": j.finished_at.isoformat() if j.finished_at else None}
            for j in rows
        ]
