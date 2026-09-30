"""Request/response models for the REST API."""

from datetime import datetime
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

from app.models import DeliveryStatus, MessageSource, TicketPriority, TicketStatus, UserRole

Password = Annotated[str, StringConstraints(min_length=10, max_length=256)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------------ users / auth


class UserBrief(ORMModel):
    id: int
    full_name: str
    email: str


class UserOut(UserBrief):
    role: UserRole
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime
    signature: str | None = None


SIGNATURE_MAX = 2000


class ProfileUpdate(BaseModel):
    """PATCH /auth/me: what users may change about themselves."""

    model_config = ConfigDict(extra="forbid")

    # Empty or whitespace-only clears the signature.
    signature: Annotated[str, StringConstraints(max_length=SIGNATURE_MAX)] | None = None

    @field_validator("signature")
    @classmethod
    def _normalize(cls, value: str | None) -> str | None:
        if value is None:
            return None
        lines = [line.rstrip() for line in value.replace("\r\n", "\n").split("\n")]
        text = "\n".join(lines).strip("\n")
        return text if text.strip() else None


class UserAdminOut(UserOut):
    """User row for the admin screen, with workload and sign-in health."""

    open_tickets: int
    recent_failed_logins: int  # within the last hour, all IPs


class RegisterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    full_name: Name
    password: Password
    role: UserRole = UserRole.AGENT


class LoginIn(BaseModel):
    email: EmailStr
    password: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class UserUpdate(BaseModel):
    """Admin PATCH /api/users/{id}. Omitted fields are unchanged."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr | None = None
    full_name: Name | None = None
    role: UserRole | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _check(self) -> "UserUpdate":
        if not self.model_fields_set:
            raise ValueError("Provide at least one of: email, full_name, role, is_active")
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class PasswordSet(BaseModel):
    """Admin password reset."""

    model_config = ConfigDict(extra="forbid")

    password: Password


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    new_password: Password

    @model_validator(mode="after")
    def _differs(self) -> "PasswordChange":
        if self.current_password == self.new_password:
            raise ValueError("new_password must differ from current_password")
        return self


class SiteSettingsOut(ORMModel):
    site_name: str
    updated_at: datetime


class SiteSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]

    @field_validator("site_name")
    @classmethod
    def _printable(cls, value: str) -> str:
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError("site_name cannot contain control characters")
        return value


class UnlockOut(BaseModel):
    cleared_failures: int


class LoginOut(BaseModel):
    user: UserOut
    # Also set as an HttpOnly cookie; the body copy is for non-browser API clients
    # (send it as `Authorization: Bearer <token>`).
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime


# ------------------------------------------------------------------ tickets


class AttachmentOut(ORMModel):
    id: int
    filename: str
    content_type: str
    size_bytes: int
    comment_id: int | None
    created_at: datetime

    @computed_field
    @property
    def download_url(self) -> str:
        return f"/api/attachments/{self.id}"


class CommentOut(ORMModel):
    id: int
    author: UserBrief | None
    author_email: str | None
    author_name: str | None
    body: str
    body_html: str | None
    is_internal: bool
    source: MessageSource
    delivery_status: DeliveryStatus | None
    delivery_error: str | None
    delivered_at: datetime | None
    delivery_attempts: int
    # Set while a failed reply is scheduled for another automatic attempt; a `failed`
    # reply with this null has given up and can be resent via .../resend.
    next_attempt_at: datetime | None
    created_at: datetime
    attachments: list[AttachmentOut] = []


class TicketSummary(ORMModel):
    id: int
    tracking_code: str
    legacy_ref: str | None  # tracking ID in the help desk it was imported from
    subject: str
    status: TicketStatus
    priority: TicketPriority
    source: MessageSource
    requester_email: str
    requester_name: str | None
    assignee: UserBrief | None
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime
    resolved_at: datetime | None


class TicketDetail(TicketSummary):
    description: str
    description_html: str | None
    attachments: list[AttachmentOut]  # attached to the opening email
    comments: list[CommentOut]         # chronological, internal notes included


class TicketPage(BaseModel):
    items: list[TicketSummary]
    total: int
    limit: int
    offset: int


class TicketUpdate(BaseModel):
    """PATCH body. Omitted fields are left unchanged; `assigned_to: null` unassigns."""

    model_config = ConfigDict(extra="forbid")

    status: TicketStatus | None = None
    priority: TicketPriority | None = None
    assigned_to: int | None = None

    @model_validator(mode="after")
    def _check(self) -> "TicketUpdate":
        if not self.model_fields_set:
            raise ValueError("Provide at least one of: status, priority, assigned_to")
        for name in ("status", "priority"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class CommentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: Annotated[str, StringConstraints(min_length=1, max_length=50_000)]
    # False = public reply, emailed to the customer. True = agents-only note.
    is_internal: bool = False
    # Optionally move the ticket in the same step (e.g. reply and set to "pending").
    status: TicketStatus | None = None
    # Append the author's saved signature (public replies only; ignored for notes).
    include_signature: bool = True

    @field_validator("body")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("body cannot be blank")
        return value.strip()


class ClaimOut(BaseModel):
    ticket: TicketSummary
    claimed: bool = Field(description="False if you already owned the ticket")
