"""Turns parsed inbound email into tickets / comments (the IMAP poll handler)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import session_scope
from app.email_service import EmailService, InboundAttachment, InboundEmail
from app.notifications import announce_new_ticket
from app.storage import write_blob
from app.models import (
    MessageSource,
    Ticket,
    TicketAttachment,
    TicketComment,
    TicketStatus,
    User,
    UserRole,
)

log = logging.getLogger(__name__)

REOPEN_ON_REPLY = {TicketStatus.RESOLVED, TicketStatus.CLOSED, TicketStatus.PENDING}


class TicketIngestor:
    def __init__(self, settings: Settings, email_service: EmailService) -> None:
        self.settings = settings
        self.email = email_service
        self.own_address = settings.smtp_from_address.lower()

    def __call__(self, inbound: InboundEmail) -> None:
        self.handle(inbound)

    def handle(self, inbound: InboundEmail) -> None:
        if inbound.is_auto_generated:
            log.info("Ignoring auto-generated mail %s from %s", inbound.message_id, inbound.from_address)
            return
        if not inbound.from_address or inbound.from_address in {self.own_address, self.settings.imap_user.lower()}:
            log.info("Ignoring mail %s from our own/empty address", inbound.message_id)
            return

        new_ticket_id: int | None = None
        try:
            with session_scope() as db:
                if self._already_ingested(db, inbound.message_id):
                    log.info("Skipping duplicate %s", inbound.message_id)
                    return
                staff = db.scalar(
                    select(User).where(User.email == inbound.from_address, User.is_active.is_(True))
                )
                ticket = self._resolve_thread(db, inbound, staff)
                if ticket is not None:
                    self._append_comment(db, ticket, inbound, staff)
                else:
                    ticket = self._create_ticket(db, inbound)
                    new_ticket_id = ticket.id
        except IntegrityError:
            # Unique Message-ID raced with a concurrent ingest; the other copy won.
            log.info("Duplicate %s detected at commit; skipping", inbound.message_id)
            return

        if new_ticket_id is not None:
            self._notify_new_ticket(new_ticket_id, inbound.priority_reasons)

    # ------------------------------------------------------------------ threading

    @staticmethod
    def _already_ingested(db: Session, message_id: str) -> bool:
        return bool(
            db.scalar(select(Ticket.id).where(Ticket.message_id == message_id))
            or db.scalar(select(TicketComment.id).where(TicketComment.message_id == message_id))
        )

    def _resolve_thread(self, db: Session, inbound: InboundEmail, staff: User | None) -> Ticket | None:
        # 1) Header threading: the reply references a message we know (inbound or one we sent).
        if ids := inbound.thread_message_ids:
            ticket = db.scalar(select(Ticket).where(Ticket.message_id.in_(ids)).limit(1))
            if ticket is None:
                ticket_id = db.scalar(
                    select(TicketComment.ticket_id).where(TicketComment.message_id.in_(ids)).limit(1)
                )
                ticket = db.get(Ticket, ticket_id) if ticket_id else None
            if ticket is not None:
                return ticket

        # 2) Subject tag [TICKET-n]. Only trusted from the requester or staff, so a stranger
        #    can't inject comments into someone else's ticket by guessing numbers.
        if (ref := inbound.ticket_ref) is not None:
            ticket = db.get(Ticket, ref)
            if ticket and (staff or inbound.from_address == ticket.requester_email):
                return ticket
            log.warning(
                "Subject references %s but sender %s is not its requester; opening a new ticket",
                ref, inbound.from_address,
            )

        # 3) Reply to an old HESK email ([#ABC-DEF-1234]) for a ticket imported from HESK,
        #    with the same sender rule HESK itself applies.
        if (legacy := inbound.legacy_ref) is not None:
            ticket = db.scalar(select(Ticket).where(Ticket.legacy_ref == legacy))
            if ticket and (staff or inbound.from_address == ticket.requester_email):
                return ticket
            if ticket:
                log.warning("HESK tag %s from %s (not its requester); opening a new ticket", legacy, inbound.from_address)
        return None

    # ------------------------------------------------------------------ writes

    def _create_ticket(self, db: Session, inbound: InboundEmail) -> Ticket:
        ticket = Ticket(
            subject=inbound.clean_subject[:998],
            description=inbound.text_body,
            description_html=inbound.html_body,
            source=MessageSource.EMAIL,
            priority=inbound.priority,  # MEDIUM unless escalated/flagged by the parser
            requester_email=inbound.from_address,
            requester_name=inbound.from_name,
            message_id=inbound.message_id,
        )
        db.add(ticket)
        db.flush()  # assigns ticket.id for attachment paths / logging
        self._store_attachments(db, ticket, None, inbound.attachments)
        if inbound.priority_reasons:
            # Audit trail so agents can see (and question) why a ticket jumped the queue.
            db.add(
                TicketComment(
                    ticket_id=ticket.id,
                    body=(
                        f"Priority automatically set to {inbound.priority.label}: "
                        + "; ".join(inbound.priority_reasons)
                    ),
                    is_internal=True,
                    source=MessageSource.SYSTEM,
                )
            )
        log.info(
            "Created %s from %s with priority %s%s",
            ticket.tracking_code, inbound.from_address, inbound.priority.value,
            f" ({'; '.join(inbound.priority_reasons)})" if inbound.priority_reasons else "",
        )
        return ticket

    def _append_comment(
        self, db: Session, ticket: Ticket, inbound: InboundEmail, staff: User | None
    ) -> TicketComment:
        comment = TicketComment(
            ticket=ticket,
            author_id=staff.id if staff else None,
            author_email=inbound.from_address,
            author_name=inbound.from_name,
            body=inbound.reply_text,
            body_html=inbound.html_body,
            # Staff replying by email (e.g. to an alert) must not look like a public
            # reply the customer received, so it becomes an internal note.
            is_internal=staff is not None,
            source=MessageSource.EMAIL,
            message_id=inbound.message_id,
            in_reply_to=inbound.in_reply_to,
            email_metadata={
                "from": inbound.from_address,
                "to": inbound.to,
                "cc": inbound.cc,
                "subject": inbound.subject,
                "references": inbound.references,
                "skipped_attachments": inbound.skipped_attachments,
            },
        )
        db.add(comment)
        db.flush()
        self._store_attachments(db, ticket, comment, inbound.attachments)

        now = datetime.now(UTC)
        ticket.last_activity_at = now
        if staff is None and ticket.status in REOPEN_ON_REPLY:
            log.info("Customer replied; reopening %s (was %s)", ticket.tracking_code, ticket.status.value)
            ticket.status = TicketStatus.OPEN
            ticket.resolved_at = None
        log.info("Appended comment %s to %s", comment.id, ticket.tracking_code)
        return comment

    def _store_attachments(
        self,
        db: Session,
        ticket: Ticket,
        comment: TicketComment | None,
        attachments: list[InboundAttachment],
    ) -> None:
        for att in attachments:
            rel_path = self._write_blob(att)
            db.add(
                TicketAttachment(
                    ticket_id=ticket.id,
                    comment_id=comment.id if comment else None,
                    filename=att.filename,
                    content_type=att.content_type,
                    size_bytes=att.size,
                    sha256=att.sha256,
                    storage_path=rel_path,
                )
            )

    def _write_blob(self, att: InboundAttachment) -> str:
        return write_blob(self.settings.attachment_dir, att.data)[0]

    # ------------------------------------------------------------------ notifications

    def _notify_new_ticket(self, ticket_id: int, priority_reasons: list[str]) -> None:
        """Runs after commit: an SMTP outage must never roll back or re-ingest a ticket."""
        announce_new_ticket(ticket_id, priority_reasons, self.email)
