"""SQLAlchemy ORM models (PostgreSQL)."""

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    SmallInteger,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, validates

# Deterministic constraint names keep Alembic migrations stable.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

TICKET_TAG_PREFIX = "TICKET"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _pg_enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    """Native PG enum that stores the lowercase `.value`, not the Python member name."""
    return Enum(enum_cls, name=name, values_callable=lambda e: [m.value for m in e])


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- enums


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    AGENT = "agent"


class TicketStatus(str, enum.Enum):
    NEW = "new"                # ingested, nobody has touched it yet
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    PENDING = "pending"        # waiting on the customer
    RESOLVED = "resolved"
    CLOSED = "closed"


class TicketPriority(str, enum.Enum):
    """Declaration order is the PostgreSQL enum order, so `ORDER BY priority DESC`
    sorts urgent -> low natively. Never reorder; only append or rename values."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"

    @property
    def label(self) -> str:
        return self.value.title()

    @property
    def rank(self) -> int:
        return list(TicketPriority).index(self)

    @classmethod
    def highest(cls, *priorities: "TicketPriority") -> "TicketPriority":
        return max(priorities, key=lambda p: p.rank)


class MessageSource(str, enum.Enum):
    EMAIL = "email"
    WEB = "web"
    SYSTEM = "system"
    IMPORT = "import"  # history brought over from another help desk (app.hesk_import)


class DeliveryStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


ACTIVE_STATUSES = (
    TicketStatus.NEW, TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.PENDING
)
CLOSED_STATUSES = (TicketStatus.RESOLVED, TicketStatus.CLOSED)
_ACTIVE_SQL = "status IN ('new', 'open', 'in_progress', 'pending')"


# --------------------------------------------------------------------------- models


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        _pg_enum(UserRole, "user_role"), nullable=False, default=UserRole.AGENT, index=True
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Appended to this agent's public replies (plain text); NULL = none.
    signature: Mapped[str | None] = mapped_column(Text)

    assigned_tickets: Mapped[list["Ticket"]] = relationship(
        back_populates="assignee", foreign_keys="Ticket.assigned_to_id"
    )

    @validates("email")
    def _normalize_email(self, _key: str, value: str) -> str:
        return value.strip().lower()

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN

    def __repr__(self) -> str:
        return f"<User {self.id} {self.email} ({self.role.value})>"


class Ticket(TimestampMixin, Base):
    __tablename__ = "tickets"

    # Starts at 10000 so public tracking codes look like [TICKET-10001].
    id: Mapped[int] = mapped_column(BigInteger, Identity(start=10000), primary_key=True)
    subject: Mapped[str] = mapped_column(String(998), nullable=False)  # RFC 5322 line limit
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    description_html: Mapped[str | None] = mapped_column(Text)  # sanitized

    status: Mapped[TicketStatus] = mapped_column(
        _pg_enum(TicketStatus, "ticket_status"),
        nullable=False,
        default=TicketStatus.NEW,
        server_default=TicketStatus.NEW.value,
    )
    priority: Mapped[TicketPriority] = mapped_column(
        _pg_enum(TicketPriority, "ticket_priority"),
        nullable=False,
        default=TicketPriority.MEDIUM,
        server_default=TicketPriority.MEDIUM.value,
    )
    source: Mapped[MessageSource] = mapped_column(
        _pg_enum(MessageSource, "message_source"), nullable=False, default=MessageSource.EMAIL
    )

    requester_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    requester_name: Mapped[str | None] = mapped_column(String(200))

    # Indexed via ix_tickets_assignee_status (leading column).
    assigned_to_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    # RFC 5322 Message-ID of the email that opened the ticket (threading root).
    message_id: Mapped[str | None] = mapped_column(String(998), unique=True)
    # Tracking ID in the help desk this ticket was imported from (e.g. HESK "ABC-DEF-1234").
    legacy_ref: Mapped[str | None] = mapped_column(String(32), unique=True)

    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # When staff were last alerted about a customer reply (throttles bursts).
    customer_reply_alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Weighted full-text search vector maintained by PostgreSQL itself.
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english', coalesce(subject, '')), 'A') || "
            "setweight(to_tsvector('english', coalesce(description, '')), 'B')",
            persisted=True,
        ),
    )

    assignee: Mapped[User | None] = relationship(
        back_populates="assigned_tickets", foreign_keys=[assigned_to_id]
    )
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_id])
    comments: Mapped[list["TicketComment"]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        passive_deletes=True,
        # Comments written in one transaction share now(); id breaks the tie.
        order_by=lambda: [TicketComment.created_at, TicketComment.id],
    )
    attachments: Mapped[list["TicketAttachment"]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        # Dashboard queue filters: "status X sorted by recent activity".
        Index("ix_tickets_status_last_activity", "status", text("last_activity_at DESC")),
        Index("ix_tickets_assignee_status", "assigned_to_id", "status"),
        # Hot path for the "unclaimed" queue; only indexes the small active subset.
        Index(
            "ix_tickets_unassigned_active",
            text("created_at DESC"),
            postgresql_where=text(f"assigned_to_id IS NULL AND {_ACTIVE_SQL}"),
        ),
        # Triage queue: active tickets, most urgent first, oldest first within a tier.
        Index(
            "ix_tickets_priority_active",
            text("priority DESC"),
            "created_at",
            postgresql_where=text(_ACTIVE_SQL),
        ),
        Index("ix_tickets_search_vector", "search_vector", postgresql_using="gin"),
    )

    @property
    def tracking_code(self) -> str:
        return f"{TICKET_TAG_PREFIX}-{self.id}"

    @property
    def subject_tag(self) -> str:
        return f"[{self.tracking_code}]"

    @validates("requester_email")
    def _normalize_email(self, _key: str, value: str) -> str:
        return value.strip().lower()

    def __repr__(self) -> str:
        return f"<Ticket {self.tracking_code} {self.status.value}>"


class TicketComment(Base):
    __tablename__ = "ticket_comments"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )

    # Staff author (NULL when the comment came from the customer).
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    author_email: Mapped[str | None] = mapped_column(String(320))
    author_name: Mapped[str | None] = mapped_column(String(200))

    body: Mapped[str] = mapped_column(Text, nullable=False)          # plaintext
    body_html: Mapped[str | None] = mapped_column(Text)              # sanitized HTML
    is_internal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    source: Mapped[MessageSource] = mapped_column(
        _pg_enum(MessageSource, "message_source"), nullable=False
    )

    # Threading: every inbound AND outbound email gets its Message-ID recorded here so a
    # customer reply to any message in the thread can be matched back to the ticket.
    message_id: Mapped[str | None] = mapped_column(String(998), unique=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(998))
    email_metadata: Mapped[dict | None] = mapped_column(JSONB)  # from/to/cc/subject/references
    # Source record of an imported comment (e.g. "hesk:reply:42"); makes re-imports idempotent.
    legacy_ref: Mapped[str | None] = mapped_column(String(64), unique=True)

    # Outbound delivery tracking (agent replies emailed to the customer).
    delivery_status: Mapped[DeliveryStatus | None] = mapped_column(
        _pg_enum(DeliveryStatus, "delivery_status")
    )
    delivery_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # When the worker should (re)try delivery; NULL = nothing scheduled (sent, internal,
    # or permanently failed). Set on creation too, as a safety net in case the web
    # process dies before its background send runs.
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    ticket: Mapped[Ticket] = relationship(back_populates="comments")
    author: Mapped[User | None] = relationship()
    attachments: Mapped[list["TicketAttachment"]] = relationship(back_populates="comment")

    __table_args__ = (
        Index("ix_ticket_comments_ticket_created", "ticket_id", "created_at"),
        # The worker's retry queue: only rows with a scheduled attempt are indexed.
        Index(
            "ix_ticket_comments_next_attempt",
            "next_attempt_at",
            postgresql_where=text("next_attempt_at IS NOT NULL"),
        ),
    )

    @property
    def is_from_customer(self) -> bool:
        return self.author_id is None and self.source == MessageSource.EMAIL


class TicketAttachment(Base):
    __tablename__ = "ticket_attachments"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    comment_id: Mapped[int | None] = mapped_column(
        ForeignKey("ticket_comments.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)  # relative to ATTACHMENT_DIR
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    ticket: Mapped[Ticket] = relationship(back_populates="attachments")
    comment: Mapped[TicketComment | None] = relationship(back_populates="attachments")


class UserSession(Base):
    """Server-side login session. Only a SHA-256 of the token is stored, so a database
    leak does not leak usable sessions; deleting the row is an immediate logout."""

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(512))

    user: Mapped[User] = relationship()


class LoginFailure(Base):
    """Append-only log of failed logins, used for sliding-window rate limits per IP,
    per account+IP and per account (see app.auth). Pruned by the worker."""

    __tablename__ = "login_failures"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(INET)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_login_failures_ip_attempted", "ip_address", "attempted_at"),
        Index("ix_login_failures_email_attempted", "email", "attempted_at"),
    )


DEFAULT_SITE_NAME = "Support Desk"


class SiteSettings(Base):
    """Site-wide settings editable by admins. Exactly one row (id = 1); add a column
    here for each new setting."""

    __tablename__ = "site_settings"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    site_name: Mapped[str] = mapped_column(
        String(100), nullable=False, default=DEFAULT_SITE_NAME, server_default=DEFAULT_SITE_NAME
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)


class CustomerLoginToken(Base):
    """One-time sign-in link emailed to a customer (only the SHA-256 is stored).
    Also serves as the log for rate-limiting link requests."""

    __tablename__ = "customer_login_tokens"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip_address: Mapped[str | None] = mapped_column(INET)

    __table_args__ = (
        Index("ix_customer_login_tokens_email_created", "email", "created_at"),
        Index("ix_customer_login_tokens_ip_created", "ip_address", "created_at"),
    )


class CustomerSession(Base):
    """Customer portal session: a verified email address, nothing more. Kept apart
    from agent sessions (own table, own cookie scoped to /portal)."""

    __tablename__ = "customer_sessions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(512))


class OutboundEmail(Base):
    """Automated emails we sent (acknowledgements, staff alerts, assignment notices).

    Remembering their Message-IDs lets a reply to one of them (for example a
    customer's out-of-office that ignores every "don't auto-reply" header) thread onto
    its ticket instead of opening a new ticket, and lets us cap acknowledgements per
    address. Agent replies are recorded on their comment instead.
    """

    __tablename__ = "outbound_emails"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(998), nullable=False, unique=True)
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id", ondelete="SET NULL"), index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # ack, new_ticket_alert, reply_alert, assignment
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_outbound_emails_recipient_kind_created", "recipient", "kind", "created_at"),)
