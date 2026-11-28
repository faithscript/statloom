"""core/services/job_state.py: claiming, leases, fencing, failure, and retry.

Navigation:
  claim              queued -> running (compare-and-set); None if someone else got it
  reclaim_expired    take over a running job whose lease expired (or fail it at the retry cap)
  heartbeat          extend the lease
  fail_job           running -> failed
  requeue            failed -> queued while attempts remain (a primitive; the policy is Component 6)
  get_job, find_expired, find_stale_queued

Concurrency model: every change is ONE `UPDATE ... WHERE <expected state>` (compare-and-set), so
of many concurrent callers exactly one matches; the others see zero rows and move on.
Fencing: `attempt_count` increases on every claim or reclaim, and every worker write
(heartbeat, fail_job, complete_job) requires `claimed_by = worker AND attempt_count = attempt`.
A worker whose lease expired and whose job was reclaimed is therefore refused (StaleClaim) and can
change nothing. It does not stop the stale worker's wasted compute, and it does not choose a lease
length (Q2). Leases use the DATABASE clock (now()), not application clocks. A worker whose lease
lapsed but whose job was NOT yet reclaimed still matches its fence and may finish: that is
intentional. The database triggers (migration 0003) enforce the same rules independently.
Never commits.
"""
import re
import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from core.models import Job
from core.services.errors import NotFound, StaleClaim

_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
MAX_MESSAGE = 500


def _positive(lease_seconds: float) -> float:
    if not lease_seconds > 0:
        raise ValueError("lease_seconds must be positive")
    return float(lease_seconds)


def _load(session: Session, job_id: uuid.UUID, row_id: uuid.UUID | None) -> Job | None:
    return None if row_id is None else session.get(Job, row_id, populate_existing=True)


def get_job(session: Session, job_id: uuid.UUID) -> Job:
    job = session.get(Job, job_id, populate_existing=True)
    if job is None:
        raise NotFound("job not found")
    return job


def claim(session: Session, job_id: uuid.UUID, worker: str, lease_seconds: float) -> Job | None:
    rid = session.execute(text(
        "UPDATE job SET status='running', claimed_by=:w, attempt_count = attempt_count + 1, "
        "lease_expires_at = now() + make_interval(secs => :lease), started_at = COALESCE(started_at, now()) "
        "WHERE id=:id AND status='queued' AND attempt_count < max_attempts RETURNING id"),
        {"id": job_id, "w": worker, "lease": _positive(lease_seconds)}).scalar()
    return _load(session, job_id, rid)


def reclaim_expired(session: Session, job_id: uuid.UUID, worker: str, lease_seconds: float) -> Job | None:
    """Take over a running job whose lease expired. Returns the job: `running` under the new owner
    (attempt_count + 1), or `failed` (`max_attempts_exceeded`) if no attempts remain. None if the job
    is not running or its lease is still valid."""
    params = {"id": job_id, "w": worker, "lease": _positive(lease_seconds)}
    rid = session.execute(text(
        "UPDATE job SET claimed_by=:w, attempt_count = attempt_count + 1, "
        "lease_expires_at = now() + make_interval(secs => :lease) "
        "WHERE id=:id AND status='running' AND lease_expires_at < now() AND attempt_count < max_attempts "
        "RETURNING id"), params).scalar()
    if rid is None:
        rid = session.execute(text(
            "UPDATE job SET status='failed', claimed_by=NULL, lease_expires_at=NULL, finished_at=now(), "
            "failure_code='max_attempts_exceeded', "
            "failure_message='the job used all of its attempts without finishing' "
            "WHERE id=:id AND status='running' AND lease_expires_at < now() AND attempt_count >= max_attempts "
            "RETURNING id"), {"id": job_id}).scalar()
    return _load(session, job_id, rid)


def _stale_or_missing(session: Session, job_id: uuid.UUID):
    if session.get(Job, job_id) is None:
        raise NotFound("job not found")
    raise StaleClaim("this worker no longer owns the job")


def heartbeat(session: Session, job_id: uuid.UUID, worker: str, attempt: int, lease_seconds: float) -> Job:
    rid = session.execute(text(
        "UPDATE job SET lease_expires_at = now() + make_interval(secs => :lease) "
        "WHERE id=:id AND status='running' AND claimed_by=:w AND attempt_count=:a RETURNING id"),
        {"id": job_id, "w": worker, "a": attempt, "lease": _positive(lease_seconds)}).scalar()
    if rid is None:
        _stale_or_missing(session, job_id)
    return _load(session, job_id, rid)


def fail_job(session: Session, job_id: uuid.UUID, worker: str, attempt: int, code: str, message: str) -> Job:
    """`code` is a short lowercase identifier from the typed errors of Components 2-4; `message` is
    their value-free message (truncated to 500 characters)."""
    if not _CODE.match(code):
        raise ValueError("failure code must match [a-z][a-z0-9_]{0,63}")
    rid = session.execute(text(
        "UPDATE job SET status='failed', claimed_by=NULL, lease_expires_at=NULL, finished_at=now(), "
        "failure_code=:code, failure_message=:msg "
        "WHERE id=:id AND status='running' AND claimed_by=:w AND attempt_count=:a RETURNING id"),
        {"id": job_id, "w": worker, "a": attempt, "code": code, "msg": message[:MAX_MESSAGE]}).scalar()
    if rid is None:
        _stale_or_missing(session, job_id)
    return _load(session, job_id, rid)


def requeue(session: Session, job_id: uuid.UUID) -> Job | None:
    """failed -> queued while attempts remain. None if the job is not failed or has hit the cap.
    Whether, when, and for which failures to call this is policy (Component 6, Q3)."""
    rid = session.execute(text(
        "UPDATE job SET status='queued', claimed_by=NULL, lease_expires_at=NULL, finished_at=NULL "
        "WHERE id=:id AND status='failed' AND attempt_count < max_attempts RETURNING id"),
        {"id": job_id}).scalar()
    return _load(session, job_id, rid)


def find_expired(session: Session, limit: int = 100) -> list[uuid.UUID]:
    """Running jobs whose lease has expired (candidates for reclaim_expired). Read-only."""
    return list(session.scalars(
        select(Job.id).where(Job.status == "running", Job.lease_expires_at < text("now()"))
        .order_by(Job.lease_expires_at).limit(limit)))


def find_stale_queued(session: Session, older_than_seconds: float, limit: int = 100) -> list[uuid.UUID]:
    """Queued jobs untouched for a while (supports any Q1 recovery option; implies no decision)."""
    return list(session.scalars(
        select(Job.id).where(Job.status == "queued",
                             Job.updated_at < text("now() - make_interval(secs => :s)").bindparams(s=older_than_seconds))
        .order_by(Job.updated_at).limit(limit)))
