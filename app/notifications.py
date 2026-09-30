"""Outbound email for agent actions, with durable retry for customer replies.

Customer replies: the API commits the comment as `pending` (with next_attempt_at as a
safety net) and schedules `send_agent_reply` as a background task, which runs after
the response is sent. Temporary SMTP failures are rescheduled with exponential
backoff and retried by the worker (`retry_due_replies`) until REPLY_MAX_ATTEMPTS,
then left `failed` for an agent to resend. Rejections (5xx) fail immediately.

Every attempt runs in its own transaction holding a row lock (SKIP LOCKED), so the
web background task and the worker sweep can never send the same reply twice at once.
"""

import logging
from datetime import UTC, datetime, timedelta
from enum import Enum
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.db import session_scope
from app.email_service import EmailService, PermanentEmailError, TransientEmailError
from app.models import DeliveryStatus, Ticket, TicketComment, User

log = logging.getLogger(__name__)

MAX_RETRY_DELAY = timedelta(hours=6)
RETRYABLE = (DeliveryStatus.PENDING, DeliveryStatus.FAILED)


class Outcome(str, Enum):
    SENT = "sent"
    RETRY = "retry"          # temporary failure, rescheduled
    FAILED = "failed"        # permanent failure or out of attempts
    SKIPPED = "skipped"      # not due / already handled / locked by someone else


@lru_cache
def get_email_service() -> EmailService:
    return EmailService(get_settings())


def _retry_delay(attempts: int) -> timedelta:
    base = timedelta(seconds=get_settings().reply_retry_base_seconds)
    return min(MAX_RETRY_DELAY, base * 2 ** max(attempts - 1, 0))


def _record_failure(comment: TicketComment, error: str, *, retryable: bool) -> Outcome:
    comment.delivery_status = DeliveryStatus.FAILED
    comment.delivery_error = error[:2000]
    if retryable and comment.delivery_attempts < get_settings().reply_max_attempts:
        comment.next_attempt_at = datetime.now(UTC) + _retry_delay(comment.delivery_attempts)
        return Outcome.RETRY
    comment.next_attempt_at = None
    return Outcome.FAILED


def deliver_reply(db: Session, comment: TicketComment) -> Outcome:
    """One delivery attempt for a locked comment row. The caller commits."""
    ticket = db.get(Ticket, comment.ticket_id, options=[selectinload(Ticket.comments)])
    author = db.get(User, comment.author_id) if comment.author_id else None
    service = get_email_service()
    comment.delivery_attempts += 1
    try:
        service.send(service.build_customer_reply(ticket, comment, author))
    except TransientEmailError as exc:
        outcome = _record_failure(comment, str(exc), retryable=True)
    except PermanentEmailError as exc:
        outcome = _record_failure(comment, str(exc), retryable=False)
    else:
        comment.delivery_status = DeliveryStatus.SENT
        comment.delivery_error = None
        comment.delivered_at = datetime.now(UTC)
        comment.next_attempt_at = None
        outcome = Outcome.SENT
    log.log(
        logging.INFO if outcome == Outcome.SENT else logging.WARNING,
        "Reply %s for %s: %s (attempt %d)%s",
        comment.id, ticket.tracking_code, outcome.value, comment.delivery_attempts,
        f", next at {comment.next_attempt_at:%H:%M:%S}" if outcome == Outcome.RETRY else "",
    )
    return outcome


def _attempt(comment_id: int, *, only_if_due: bool) -> Outcome:
    try:
        with session_scope() as db:
            comment = db.scalar(
                select(TicketComment)
                .where(TicketComment.id == comment_id)
                .with_for_update(skip_locked=True)
            )
            if comment is None or comment.delivery_status not in RETRYABLE:
                return Outcome.SKIPPED
            if only_if_due and (
                comment.next_attempt_at is None or comment.next_attempt_at > datetime.now(UTC)
            ):
                return Outcome.SKIPPED
            return deliver_reply(db, comment)
    except Exception:
        # A bug (not an SMTP error): count the attempt so a poison row can't loop forever.
        log.exception("Reply %s: unexpected error during delivery", comment_id)
        with session_scope() as db:
            comment = db.get(TicketComment, comment_id)
            if comment is not None and comment.delivery_status in RETRYABLE:
                comment.delivery_attempts += 1
                return _record_failure(
                    comment, "Internal error while sending; see server logs", retryable=True
                )
        return Outcome.FAILED


def send_agent_reply(comment_id: int) -> None:
    """Background task scheduled by the API right after a public reply is committed."""
    _attempt(comment_id, only_if_due=False)


def retry_due_replies(limit: int = 20) -> int:
    """Worker sweep: retry replies whose next_attempt_at has passed. Stops early on a
    temporary failure, since SMTP is probably down and the rest would fail too."""
    with session_scope() as db:
        due = db.scalars(
            select(TicketComment.id)
            .where(
                TicketComment.next_attempt_at <= datetime.now(UTC),
                TicketComment.delivery_status.in_(RETRYABLE),
            )
            .order_by(TicketComment.next_attempt_at)
            .limit(limit)
        ).all()
    sent = 0
    for comment_id in due:
        outcome = _attempt(comment_id, only_if_due=True)
        sent += outcome == Outcome.SENT
        if outcome == Outcome.RETRY:
            break
    return sent


def pending_deadline() -> datetime:
    """next_attempt_at for a fresh reply: the sweep takes over if the background task
    never ran (e.g. the web container restarted)."""
    return datetime.now(UTC) + timedelta(seconds=get_settings().reply_pending_timeout_seconds)


def notify_assignee(ticket_id: int, assignee_id: int, actor_id: int | None) -> None:
    """Tell the newly assigned agent about the ticket (best effort, not retried)."""
    try:
        with session_scope() as db:
            ticket = db.get(Ticket, ticket_id)
            assignee = db.get(User, assignee_id)
            actor = db.get(User, actor_id) if actor_id else None
            if ticket is None or assignee is None or not assignee.is_active:
                return
            if ticket.assigned_to_id != assignee_id:
                return  # re-assigned again before we got here; that change notifies instead
            get_email_service().notify_assignment(ticket, assignee, actor)
    except Exception:
        log.exception("Assignment notice for ticket %s crashed", ticket_id)
