"""Customer portal API (/portal/api/*): sign in with an emailed link, open tickets,
follow the conversation, reply, and mark tickets solved.

Isolation rules, enforced in every query here:
  * a customer is a verified email address; they only ever see tickets whose
    requester_email is that address (anything else is a 404, never a 403);
  * internal notes and system/audit entries are never returned;
  * portal sessions use their own table and a cookie scoped to /portal, so agent
    and customer sign-ins can't leak into each other.
"""

from __future__ import annotations

import logging
import mimetypes
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, EmailStr, Field, computed_field
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.db import get_db
from app.email_service import assess_priority, safe_filename
from app.errors import APIError
from app.models import (
    ACTIVE_STATUSES,
    CustomerLoginToken,
    CustomerSession,
    MessageSource,
    Ticket,
    TicketAttachment,
    TicketComment,
    TicketStatus,
)
from app.notifications import alert_customer_reply, announce_new_ticket, get_email_service
from app.security import hash_token, new_session_token
from app.site import current_site_name
from app.storage import write_blob

log = logging.getLogger(__name__)

COOKIE_NAME = "tts_portal"
COOKIE_PATH = "/portal"
REOPEN_ON_REPLY = {TicketStatus.PENDING, TicketStatus.RESOLVED, TicketStatus.CLOSED}
HOUR = timedelta(hours=1)

# What customers see instead of internal workflow states.
STATUS_LABELS = {
    TicketStatus.NEW: "Received",
    TicketStatus.OPEN: "In progress",
    TicketStatus.IN_PROGRESS: "In progress",
    TicketStatus.PENDING: "Awaiting your reply",
    TicketStatus.RESOLVED: "Resolved",
    TicketStatus.CLOSED: "Closed",
}


def _require_enabled() -> None:
    if not get_settings().portal_enabled:
        raise APIError(status.HTTP_404_NOT_FOUND, "Not Found")


router = APIRouter(prefix="/portal/api", tags=["portal"], dependencies=[Depends(_require_enabled)])
DB = Annotated[Session, Depends(get_db)]


# ------------------------------------------------------------------ schemas


class LinkRequest(BaseModel):
    email: EmailStr


class VerifyIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)


class MeOut(BaseModel):
    email: str
    name: str | None


class PortalAttachment(BaseModel):
    id: int
    filename: str
    content_type: str
    size_bytes: int

    @computed_field
    @property
    def download_url(self) -> str:
        return f"/portal/api/attachments/{self.id}"


class PortalMessage(BaseModel):
    id: str
    from_customer: bool
    author_name: str
    body: str
    created_at: datetime
    attachments: list[PortalAttachment]


class PortalTicketSummary(BaseModel):
    id: int
    reference: str
    subject: str
    status: TicketStatus
    status_label: str
    is_open: bool
    created_at: datetime
    last_activity_at: datetime


class PortalTicket(PortalTicketSummary):
    messages: list[PortalMessage]


# ------------------------------------------------------------------ sessions


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def current_customer(request: Request, db: DB) -> CustomerSession:
    token = request.cookies.get(COOKIE_NAME)
    now = datetime.now(UTC)
    session = None
    if token:
        session = db.scalar(select(CustomerSession).where(
            CustomerSession.token_hash == hash_token(token), CustomerSession.expires_at > now))
    if session is None:
        raise APIError(status.HTTP_401_UNAUTHORIZED, "Please sign in")
    if not session.last_seen_at or now - session.last_seen_at > timedelta(minutes=5):
        session.last_seen_at = now
    return session


Customer = Annotated[CustomerSession, Depends(current_customer)]


def _too_many(message: str) -> APIError:
    return APIError(status.HTTP_429_TOO_MANY_REQUESTS, message, headers={"Retry-After": "3600"})


# ------------------------------------------------------------------ helpers


def _summary(t: Ticket) -> dict:
    return {
        "id": t.id,
        "reference": t.legacy_ref or t.tracking_code,
        "subject": t.subject,
        "status": t.status,
        "status_label": STATUS_LABELS[t.status],
        "is_open": t.status in ACTIVE_STATUSES,
        "created_at": t.created_at,
        "last_activity_at": t.last_activity_at,
    }


def _attachments(items: list[TicketAttachment]) -> list[PortalAttachment]:
    return [PortalAttachment(id=a.id, filename=a.filename, content_type=a.content_type, size_bytes=a.size_bytes) for a in items]


def _customer_ticket(db: Session, ticket_id: int, email: str, *, for_update: bool = False) -> Ticket:
    stmt = select(Ticket).where(Ticket.id == ticket_id, Ticket.requester_email == email)
    if for_update:
        stmt = stmt.with_for_update()
    ticket = db.scalar(stmt)
    if ticket is None:
        raise APIError(status.HTTP_404_NOT_FOUND, "Ticket not found")
    return ticket


def _ticket_view(db: Session, ticket_id: int, email: str) -> PortalTicket:
    ticket = db.scalar(
        select(Ticket)
        .where(Ticket.id == ticket_id, Ticket.requester_email == email)
        .options(
            selectinload(Ticket.attachments),
            selectinload(Ticket.comments).selectinload(TicketComment.author),
            selectinload(Ticket.comments).selectinload(TicketComment.attachments),
        )
        .execution_options(populate_existing=True)
    )
    if ticket is None:
        raise APIError(status.HTTP_404_NOT_FOUND, "Ticket not found")
    customer_name = ticket.requester_name or "You"
    messages = [PortalMessage(
        id="original",
        from_customer=True,
        author_name=customer_name,
        body=ticket.description,
        created_at=ticket.created_at,
        attachments=_attachments([a for a in ticket.attachments if a.comment_id is None]),
    )]
    for c in ticket.comments:
        if c.is_internal or c.source == MessageSource.SYSTEM:
            continue  # agents-only
        from_customer = c.author_id is None
        name = (c.author_name or customer_name) if from_customer else (c.author.full_name if c.author else c.author_name or "Support")
        messages.append(PortalMessage(
            id=str(c.id), from_customer=from_customer, author_name=name, body=c.body,
            created_at=c.created_at, attachments=_attachments(c.attachments),
        ))
    return PortalTicket(**_summary(ticket), messages=messages)


def _read_uploads(files: list[UploadFile]) -> list[tuple[str, str, bytes]]:
    s = get_settings()
    files = [f for f in files if f and f.filename]
    if len(files) > s.portal_max_files:
        raise APIError(status.HTTP_400_BAD_REQUEST, f"You can attach up to {s.portal_max_files} files")
    out = []
    for i, f in enumerate(files, 1):
        data = f.file.read(s.attachment_max_bytes + 1)
        name = safe_filename(f.filename, f"attachment-{i}")
        if len(data) > s.attachment_max_bytes:
            raise APIError(status.HTTP_400_BAD_REQUEST,
                           f"“{name}” is larger than {s.attachment_max_bytes // (1024 * 1024)} MB")
        content_type = f.content_type if f.content_type and "/" in f.content_type else None
        out.append((name, content_type or mimetypes.guess_type(name)[0] or "application/octet-stream", data))
    return out


def _store(db: Session, ticket: Ticket, comment: TicketComment | None, uploads: list[tuple[str, str, bytes]]) -> None:
    root = get_settings().attachment_dir
    for name, content_type, data in uploads:
        rel_path, digest = write_blob(root, data)
        db.add(TicketAttachment(
            ticket_id=ticket.id, comment_id=comment.id if comment else None, filename=name,
            content_type=content_type, size_bytes=len(data), sha256=digest, storage_path=rel_path,
        ))


def _text(value: str, field: str, max_len: int) -> str:
    value = (value or "").strip()
    if not value:
        raise APIError(status.HTTP_400_BAD_REQUEST, "Validation failed", [{"field": field, "message": "This field is required"}])
    if len(value) > max_len:
        raise APIError(status.HTTP_400_BAD_REQUEST, "Validation failed",
                       [{"field": field, "message": f"Must be at most {max_len} characters"}])
    return value


# ------------------------------------------------------------------ sign-in


def _send_link(email: str, link: str) -> None:
    try:
        get_email_service().send_portal_link(email, link, current_site_name(), get_settings().portal_link_minutes)
    except Exception:
        log.exception("Could not send portal sign-in link")


@router.post("/request-link", status_code=status.HTTP_202_ACCEPTED)
def request_link(payload: LinkRequest, request: Request, db: DB, background: BackgroundTasks) -> dict:
    """Email a one-time sign-in link. The reply is the same whether or not the address
    has tickets (anyone may use the portal), so it reveals nothing."""
    s = get_settings()
    email = payload.email.lower()
    ip = _client_ip(request)
    since = datetime.now(UTC) - HOUR
    per_email = db.scalar(select(func.count()).select_from(CustomerLoginToken)
                          .where(CustomerLoginToken.email == email, CustomerLoginToken.created_at > since))
    per_ip = db.scalar(select(func.count()).select_from(CustomerLoginToken)
                       .where(CustomerLoginToken.ip_address == ip, CustomerLoginToken.created_at > since)) if ip else 0
    if per_email >= s.portal_links_per_hour or per_ip >= s.portal_links_per_ip_per_hour:
        raise _too_many("Too many sign-in emails were requested. Please try again later.")

    token = new_session_token()
    db.add(CustomerLoginToken(
        email=email, token_hash=hash_token(token), ip_address=ip,
        expires_at=datetime.now(UTC) + timedelta(minutes=s.portal_link_minutes),
    ))
    # The token travels in the URL fragment: it isn't sent to servers or logged, and
    # the page asks for a click before using it, so link scanners can't consume it.
    link = f"{s.app_base_url.rstrip('/')}/portal/#/verify/{token}"
    background.add_task(_send_link, email, link)
    return {"message": "Check your email for a sign-in link."}


@router.post("/verify", response_model=MeOut)
def verify(payload: VerifyIn, request: Request, response: Response, db: DB) -> MeOut:
    s = get_settings()
    now = datetime.now(UTC)
    row = db.scalar(
        select(CustomerLoginToken)
        .where(CustomerLoginToken.token_hash == hash_token(payload.token),
               CustomerLoginToken.used_at.is_(None), CustomerLoginToken.expires_at > now)
        .with_for_update()
    )
    if row is None:
        raise APIError(status.HTTP_400_BAD_REQUEST, "This sign-in link is invalid, already used or expired. Please request a new one.")
    # Using one link retires every other outstanding link for this address.
    db.execute(update(CustomerLoginToken)
               .where(CustomerLoginToken.email == row.email, CustomerLoginToken.used_at.is_(None))
               .values(used_at=now))
    token = new_session_token()
    expires_at = now + timedelta(days=s.portal_session_days)
    db.add(CustomerSession(
        email=row.email, token_hash=hash_token(token), expires_at=expires_at, last_seen_at=now,
        ip_address=_client_ip(request), user_agent=(request.headers.get("user-agent") or "")[:512] or None,
    ))
    response.set_cookie(COOKIE_NAME, token, expires=expires_at, path=COOKIE_PATH,
                        httponly=True, secure=s.cookie_secure, samesite="lax")
    return MeOut(email=row.email, name=_customer_name(db, row.email))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, db: DB) -> Response:
    if token := request.cookies.get(COOKIE_NAME):
        db.execute(delete(CustomerSession).where(CustomerSession.token_hash == hash_token(token)))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH, httponly=True, secure=get_settings().cookie_secure, samesite="lax")
    return response


def _customer_name(db: Session, email: str) -> str | None:
    return db.scalar(
        select(Ticket.requester_name)
        .where(Ticket.requester_email == email, Ticket.requester_name.is_not(None))
        .order_by(Ticket.created_at.desc()).limit(1)
    )


@router.get("/me", response_model=MeOut)
def me(customer: Customer, db: DB) -> MeOut:
    return MeOut(email=customer.email, name=_customer_name(db, customer.email))


# ------------------------------------------------------------------ tickets


@router.get("/tickets", response_model=list[PortalTicketSummary])
def list_tickets(customer: Customer, db: DB) -> list[PortalTicketSummary]:
    """All of this customer's tickets (emailed, portal and imported), newest activity first."""
    tickets = db.scalars(
        select(Ticket).where(Ticket.requester_email == customer.email)
        .order_by(Ticket.last_activity_at.desc()).limit(500)
    )
    return [PortalTicketSummary(**_summary(t)) for t in tickets]


@router.get("/tickets/{ticket_id}", response_model=PortalTicket)
def get_ticket(ticket_id: int, customer: Customer, db: DB) -> PortalTicket:
    return _ticket_view(db, ticket_id, customer.email)


@router.post("/tickets", response_model=PortalTicket, status_code=status.HTTP_201_CREATED)
def create_ticket(
    customer: Customer,
    db: DB,
    background: BackgroundTasks,
    subject: Annotated[str, Form()] = "",
    message: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    files: Annotated[list[UploadFile], File()] = [],
) -> PortalTicket:
    s = get_settings()
    subject = _text(subject, "subject", 200)
    message = _text(message, "message", 50_000)
    name = name.strip()[:200] or _customer_name(db, customer.email)
    recent = db.scalar(select(func.count()).select_from(Ticket).where(
        Ticket.requester_email == customer.email, Ticket.source == MessageSource.WEB,
        Ticket.created_at > datetime.now(UTC) - HOUR))
    if recent >= s.portal_tickets_per_hour:
        raise _too_many("You've opened several tickets in the last hour. Please try again later or reply to an existing ticket.")
    uploads = _read_uploads(files)

    assessment = assess_priority(subject, message, keywords_enabled=s.priority_escalation_enabled)
    ticket = Ticket(
        subject=subject, description=message, source=MessageSource.WEB, priority=assessment.priority,
        requester_email=customer.email, requester_name=name,
    )
    db.add(ticket)
    db.flush()
    _store(db, ticket, None, uploads)
    if assessment.reasons:
        db.add(TicketComment(
            ticket_id=ticket.id, is_internal=True, source=MessageSource.SYSTEM,
            body=f"Priority automatically set to {assessment.priority.label}: " + "; ".join(assessment.reasons),
        ))
    db.flush()
    # Agent alert + customer acknowledgement, after the request has committed.
    background.add_task(announce_new_ticket, ticket.id, assessment.reasons)
    return _ticket_view(db, ticket.id, customer.email)


@router.post("/tickets/{ticket_id}/messages", response_model=PortalTicket, status_code=status.HTTP_201_CREATED)
def reply(
    ticket_id: int,
    customer: Customer,
    db: DB,
    background: BackgroundTasks,
    message: Annotated[str, Form()] = "",
    files: Annotated[list[UploadFile], File()] = [],
) -> PortalTicket:
    s = get_settings()
    ticket = _customer_ticket(db, ticket_id, customer.email, for_update=True)
    message = _text(message, "message", 50_000)
    recent = db.scalar(select(func.count()).select_from(TicketComment).where(
        TicketComment.author_email == customer.email, TicketComment.source == MessageSource.WEB,
        TicketComment.author_id.is_(None), TicketComment.created_at > datetime.now(UTC) - HOUR))
    if recent >= s.portal_replies_per_hour:
        raise _too_many("You've sent a lot of messages in the last hour. Please try again later.")
    uploads = _read_uploads(files)

    comment = TicketComment(
        ticket_id=ticket.id, author_email=customer.email, author_name=ticket.requester_name,
        body=message, is_internal=False, source=MessageSource.WEB,
    )
    db.add(comment)
    db.flush()
    _store(db, ticket, comment, uploads)
    ticket.last_activity_at = datetime.now(UTC)
    reopened = ticket.status in REOPEN_ON_REPLY
    if reopened:  # same rule as a customer reply by email
        ticket.status = TicketStatus.OPEN
        ticket.resolved_at = None
    db.flush()
    background.add_task(alert_customer_reply, comment.id, reopened)  # after commit
    return _ticket_view(db, ticket.id, customer.email)


@router.post("/tickets/{ticket_id}/close", response_model=PortalTicket)
def close_ticket(ticket_id: int, customer: Customer, db: DB) -> PortalTicket:
    """'This is solved': mark the ticket resolved. Replying later reopens it."""
    ticket = _customer_ticket(db, ticket_id, customer.email, for_update=True)
    if ticket.status in ACTIVE_STATUSES:
        now = datetime.now(UTC)
        previous = ticket.status
        ticket.status = TicketStatus.RESOLVED
        ticket.resolved_at = now
        ticket.last_activity_at = now
        db.add(TicketComment(
            ticket_id=ticket.id, is_internal=True, source=MessageSource.SYSTEM,
            body=f"Customer marked the ticket as solved in the portal (status {previous.value} → resolved)",
        ))
        db.flush()
    return _ticket_view(db, ticket.id, customer.email)


@router.get("/attachments/{attachment_id}", response_class=FileResponse)
def download(attachment_id: int, customer: Customer, db: DB) -> FileResponse:
    att = db.scalar(
        select(TicketAttachment).join(Ticket, Ticket.id == TicketAttachment.ticket_id)
        .where(TicketAttachment.id == attachment_id, Ticket.requester_email == customer.email)
    )
    if att is not None and att.comment_id is not None:
        comment = db.get(TicketComment, att.comment_id)
        if comment is None or comment.is_internal or comment.source == MessageSource.SYSTEM:
            att = None  # files on internal notes stay internal
    root = get_settings().attachment_dir.resolve()
    path = (root / att.storage_path).resolve() if att else None
    if att is None or path is None or not path.is_relative_to(root) or not path.is_file():
        raise APIError(status.HTTP_404_NOT_FOUND, "Attachment not found")
    return FileResponse(
        path, media_type=att.content_type, filename=att.filename,
        headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"},
    )
