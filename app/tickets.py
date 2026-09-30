"""Ticket, comment and attachment routes under /api (all require authentication)."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.auth import CurrentUser, current_user
from app.config import get_settings
from app.db import get_db
from app.errors import APIError
from app.models import (
    CLOSED_STATUSES,
    DeliveryStatus,
    MessageSource,
    Ticket,
    TicketAttachment,
    TicketComment,
    TicketPriority,
    TicketStatus,
    User,
)
from app.notifications import notify_assignee, pending_deadline, send_agent_reply
from app.schemas import (
    ClaimOut,
    CommentCreate,
    CommentOut,
    TicketDetail,
    TicketPage,
    TicketSummary,
    TicketUpdate,
)

# Router-level dependency: every route below rejects unauthenticated requests with
# 401, even if a handler forgets to ask for the user.
router = APIRouter(prefix="/api", tags=["tickets"], dependencies=[Depends(current_user)])

DB = Annotated[Session, Depends(get_db)]
TicketSort = Literal[
    "-last_activity_at", "last_activity_at", "-created_at", "created_at", "-priority", "priority"
]
SORTS = {
    "-last_activity_at": [Ticket.last_activity_at.desc()],
    "last_activity_at": [Ticket.last_activity_at.asc()],
    "-created_at": [Ticket.created_at.desc()],
    "created_at": [Ticket.created_at.asc()],
    # Enum order is low < medium < high < urgent; oldest first within a tier.
    "-priority": [Ticket.priority.desc(), Ticket.created_at.asc()],
    "priority": [Ticket.priority.asc(), Ticket.created_at.asc()],
}


# ------------------------------------------------------------------ helpers


def _get_ticket(db: Session, ticket_id: int, *, for_update: bool = False) -> Ticket:
    stmt = select(Ticket).where(Ticket.id == ticket_id)
    if for_update:
        # Serializes concurrent edits (two agents claiming/assigning at once) so each
        # sees the other's result and notifications fire exactly once per change.
        stmt = stmt.with_for_update()
    ticket = db.scalar(stmt)
    if ticket is None:
        raise APIError(status.HTTP_404_NOT_FOUND, "Ticket not found")
    return ticket


def _set_status(ticket: Ticket, new: TicketStatus, now: datetime) -> str | None:
    """Apply a status change; returns an audit line, or None if nothing changed."""
    old = ticket.status
    if old == new:
        return None
    ticket.status = new
    if new in CLOSED_STATUSES:
        ticket.resolved_at = ticket.resolved_at or now
    else:
        ticket.resolved_at = None
    return f"status {old.value} → {new.value}"


def _audit(db: Session, ticket: Ticket, actor: User, changes: list[str]) -> None:
    """Record metadata changes as an internal system note in the ticket history."""
    if changes:
        db.add(
            TicketComment(
                ticket_id=ticket.id,
                author_id=actor.id,
                body=f"{actor.full_name} changed " + "; ".join(changes),
                is_internal=True,
                source=MessageSource.SYSTEM,
            )
        )


def _parse_assigned_to(value: str | None, user: User) -> int | Literal["none"] | None:
    if value is None:
        return None
    value = value.strip().lower()
    if value == "me":
        return user.id
    if value in ("none", "unassigned"):
        return "none"
    if value.isdigit():
        return int(value)
    raise APIError(
        status.HTTP_400_BAD_REQUEST,
        "Validation failed",
        [{"field": "assigned_to", "message": "Use a user id, 'me' or 'none'"}],
    )


# ------------------------------------------------------------------ tickets


@router.get("/tickets", response_model=TicketPage)
def list_tickets(
    db: DB,
    user: CurrentUser,
    status_: Annotated[list[TicketStatus] | None, Query(alias="status")] = None,
    priority: Annotated[list[TicketPriority] | None, Query()] = None,
    assigned_to: Annotated[str | None, Query(description="User id, 'me' or 'none'")] = None,
    q: Annotated[str | None, Query(max_length=200, description="Full-text search")] = None,
    sort: TicketSort = "-last_activity_at",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TicketPage:
    """List tickets. Repeat `status`/`priority` to match any of several values,
    e.g. `?status=new&status=open&assigned_to=me`."""
    filters = []
    if status_:
        filters.append(Ticket.status.in_(status_))
    if priority:
        filters.append(Ticket.priority.in_(priority))
    match _parse_assigned_to(assigned_to, user):
        case "none":
            filters.append(Ticket.assigned_to_id.is_(None))
        case int(agent_id):
            filters.append(Ticket.assigned_to_id == agent_id)
    if q and q.strip():
        filters.append(or_(
            Ticket.search_vector.op("@@")(func.websearch_to_tsquery("english", q)),
            Ticket.legacy_ref == q.strip().strip("[]#").upper(),  # e.g. a HESK tracking ID
        ))

    total = db.scalar(select(func.count()).select_from(Ticket).where(*filters))
    tickets = db.scalars(
        select(Ticket)
        .where(*filters)
        .options(joinedload(Ticket.assignee))
        .order_by(*SORTS[sort], Ticket.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return TicketPage(
        items=[TicketSummary.model_validate(t) for t in tickets],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.get("/tickets/{ticket_id}", response_model=TicketDetail)
def get_ticket(ticket_id: int, db: DB) -> TicketDetail:
    """A ticket with its full chronological history (replies, internal notes,
    system audit entries) and attachment metadata."""
    ticket = db.scalar(
        select(Ticket)
        .where(Ticket.id == ticket_id)
        .options(
            joinedload(Ticket.assignee),
            selectinload(Ticket.attachments),
            selectinload(Ticket.comments).joinedload(TicketComment.author),
            selectinload(Ticket.comments).selectinload(TicketComment.attachments),
        )
    )
    if ticket is None:
        raise APIError(status.HTTP_404_NOT_FOUND, "Ticket not found")
    detail = TicketDetail.model_validate(ticket)
    # Comment attachments are nested under their comment; keep the top level for
    # files that came with the opening email.
    detail.attachments = [a for a in detail.attachments if a.comment_id is None]
    return detail


@router.patch("/tickets/{ticket_id}", response_model=TicketSummary)
def update_ticket(
    ticket_id: int,
    payload: TicketUpdate,
    db: DB,
    user: CurrentUser,
    background: BackgroundTasks,
) -> TicketSummary:
    """Change status, priority and/or assignee. When `assigned_to` changes to another
    agent, that agent is emailed (after commit). `assigned_to: null` unassigns."""
    ticket = _get_ticket(db, ticket_id, for_update=True)
    fields = payload.model_fields_set
    now = datetime.now(UTC)
    changes: list[str] = []

    if "assigned_to" in fields and payload.assigned_to != ticket.assigned_to_id:
        assignee = None
        if payload.assigned_to is not None:
            assignee = db.get(User, payload.assigned_to)
            if assignee is None or not assignee.is_active:
                raise APIError(
                    status.HTTP_400_BAD_REQUEST,
                    "Validation failed",
                    [{"field": "assigned_to", "message": "Must be the id of an active user"}],
                )
        previous = ticket.assignee
        ticket.assignee = assignee
        changes.append(
            f"assignee {previous.full_name if previous else 'nobody'} → "
            f"{assignee.full_name if assignee else 'nobody'}"
        )
        if assignee is not None and assignee.id != user.id:  # no email for self-assignment
            background.add_task(notify_assignee, ticket.id, assignee.id, user.id)
        if assignee is not None and ticket.status == TicketStatus.NEW and "status" not in fields:
            changes.append(_set_status(ticket, TicketStatus.OPEN, now))

    if "priority" in fields and payload.priority != ticket.priority:
        changes.append(f"priority {ticket.priority.value} → {payload.priority.value}")
        ticket.priority = payload.priority

    if "status" in fields and (line := _set_status(ticket, payload.status, now)):
        changes.append(line)

    _audit(db, ticket, user, changes)
    db.flush()
    return TicketSummary.model_validate(ticket)


@router.post("/tickets/{ticket_id}/claim", response_model=ClaimOut)
def claim_ticket(ticket_id: int, db: DB, user: CurrentUser) -> ClaimOut:
    """Assign an unassigned ticket to yourself. 409 if another agent owns it —
    use PATCH to deliberately take it over."""
    ticket = _get_ticket(db, ticket_id, for_update=True)
    if ticket.assigned_to_id == user.id:
        return ClaimOut(ticket=TicketSummary.model_validate(ticket), claimed=False)
    if ticket.assigned_to_id is not None:
        raise APIError(
            status.HTTP_409_CONFLICT,
            f"Ticket is already assigned to {ticket.assignee.full_name}",
            {"assigned_to": ticket.assigned_to_id},
        )
    ticket.assignee = user
    changes = ["assignee nobody → " + user.full_name + " (claimed)"]
    if ticket.status == TicketStatus.NEW:
        changes.append(_set_status(ticket, TicketStatus.OPEN, datetime.now(UTC)))
    _audit(db, ticket, user, changes)
    db.flush()
    return ClaimOut(ticket=TicketSummary.model_validate(ticket), claimed=True)


# ------------------------------------------------------------------ comments


@router.post(
    "/tickets/{ticket_id}/comments",
    response_model=CommentOut,
    status_code=status.HTTP_201_CREATED,
)
def add_comment(
    ticket_id: int,
    payload: CommentCreate,
    db: DB,
    user: CurrentUser,
    background: BackgroundTasks,
) -> CommentOut:
    """Add an internal note (`is_internal: true`) or a public reply (default).

    Public replies are emailed to the customer by a background task once this request
    has committed; the response has `delivery_status: "pending"`, and re-fetching the
    ticket shows `sent` or `failed` (with `delivery_error`). Temporary SMTP failures are
    retried automatically by the worker (see `next_attempt_at`).
    """
    ticket = _get_ticket(db, ticket_id, for_update=True)
    now = datetime.now(UTC)
    comment = TicketComment(
        ticket_id=ticket.id,
        author=user,
        author_email=user.email,
        author_name=user.full_name,
        body=payload.body,
        is_internal=payload.is_internal,
        source=MessageSource.WEB,
        delivery_status=None if payload.is_internal else DeliveryStatus.PENDING,
        next_attempt_at=None if payload.is_internal else pending_deadline(),
    )
    db.add(comment)
    ticket.last_activity_at = now

    # Any agent response takes a ticket out of NEW, unless a status was given.
    target = payload.status or (TicketStatus.OPEN if ticket.status == TicketStatus.NEW else None)
    db.flush()  # comment gets its id before the audit note, keeping history ordered
    if target and (line := _set_status(ticket, target, now)):
        _audit(db, ticket, user, [line])
    db.flush()

    if not payload.is_internal:
        background.add_task(send_agent_reply, comment.id)
    return CommentOut.model_validate(comment)


@router.post(
    "/tickets/{ticket_id}/comments/{comment_id}/resend",
    response_model=CommentOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def resend_reply(
    ticket_id: int, comment_id: int, db: DB, background: BackgroundTasks
) -> CommentOut:
    """Retry emailing a public reply whose delivery failed. Starts a fresh retry
    budget; the same Message-ID is reused so the customer's thread stays intact."""
    comment = db.scalar(
        select(TicketComment)
        .where(TicketComment.id == comment_id, TicketComment.ticket_id == ticket_id)
        .with_for_update()
    )
    if comment is None:
        raise APIError(status.HTTP_404_NOT_FOUND, "Comment not found")
    if comment.is_internal or comment.delivery_status is None:
        raise APIError(status.HTTP_400_BAD_REQUEST, "Only public replies are emailed")
    if comment.delivery_status == DeliveryStatus.SENT:
        raise APIError(status.HTTP_409_CONFLICT, "Reply was already delivered")
    if comment.delivery_status == DeliveryStatus.PENDING:
        raise APIError(status.HTTP_409_CONFLICT, "Delivery is already in progress")

    comment.delivery_status = DeliveryStatus.PENDING
    comment.delivery_attempts = 0
    comment.next_attempt_at = pending_deadline()
    db.flush()
    background.add_task(send_agent_reply, comment.id)
    return CommentOut.model_validate(comment)


# ------------------------------------------------------------------ attachments


@router.get("/attachments/{attachment_id}", response_class=FileResponse)
def download_attachment(attachment_id: int, db: DB) -> FileResponse:
    att = db.get(TicketAttachment, attachment_id)
    root = get_settings().attachment_dir.resolve()
    path = (root / att.storage_path).resolve() if att else None
    if att is None or path is None or not path.is_relative_to(root) or not path.is_file():
        raise APIError(status.HTTP_404_NOT_FOUND, "Attachment not found")
    return FileResponse(
        path,
        media_type=att.content_type,
        filename=att.filename,  # sent as Content-Disposition: attachment
        headers={
            # Customer-supplied files must never render as active content on our origin.
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
    )

