"""Configurable IMAP ingestion + SMTP delivery.

The service is transport-only: it knows how to talk to mail servers, parse/sanitize
messages, and compose thread-preserving outbound mail. Persisting tickets is the job of
`app.ingestion.TicketIngestor`, which is passed in as the per-message handler.
"""

from __future__ import annotations

import hashlib
import html
import imaplib
import logging
import random
import re
import smtplib
import ssl
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parseaddr
from typing import TYPE_CHECKING, TypeVar

import nh3

from app.config import Settings
from app.models import TICKET_TAG_PREFIX, DeliveryStatus, TicketPriority

if TYPE_CHECKING:
    from app.models import Ticket, TicketComment, User

log = logging.getLogger(__name__)

T = TypeVar("T")

TICKET_REF_RE = re.compile(rf"\[{TICKET_TAG_PREFIX}-(\d+)\]", re.IGNORECASE)
# Tracking tag in emails sent by HESK, for tickets imported from it (see app.hesk_import).
HESK_REF_RE = re.compile(r"\[#([A-Z0-9]{3}-[A-Z0-9]{3}-[A-Z0-9]{4})\]")
MSGID_RE = re.compile(r"<[^<>\s]+>")
SUBJECT_PREFIX_RE = re.compile(r"^\s*((re|fwd?|aw|sv|antw)\s*(\[\d+\])?\s*:\s*)+", re.IGNORECASE)
REPLY_MARKER = "##- Please type your reply above this line -##"

# Lines that start the quoted part of a reply in the common mail clients.
QUOTE_HEADER_RES = [
    re.compile(re.escape(REPLY_MARKER)),
    re.compile(r"^On\b[^\n]{0,300}(?:\n[^\n]{0,300})?\bwrote:\s*$", re.MULTILINE),  # Gmail/Apple
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.MULTILINE | re.IGNORECASE),  # Outlook
    re.compile(r"^_{20,}\s*\n(?:From|Von|De):", re.MULTILINE),  # Outlook web
    re.compile(r"^From:\s[^\n]+\n(?:Sent|Date):\s", re.MULTILINE),  # Outlook desktop
]

ALLOWED_HTML_TAGS = nh3.ALLOWED_TAGS - {"img"}  # no remote images = no tracking pixels
STRIPPED_CONTENT_TAGS = {"script", "style", "head", "title"}
QUARANTINE_FLAGS = r"(\Seen \Flagged)"
MAX_REFERENCES = 20

# --- Priority escalation ---------------------------------------------------------
# Strong terms: URGENT in the subject, HIGH in the body (bodies are noisier:
# signatures, disclaimers, forwarded text). Weak terms only count in the subject.
_DOWN_SUBJECTS = (
    r"site|website|server|servers|system|systems|service|services|production|prod|"
    r"network|internet|app|application|portal|email|e-mail|vpn|database|db|api|everything"
)
STRONG_PRIORITY_RE = re.compile(
    r"\b(urgent|emergency|critical|outage|sev[\s-]?1|p1)\b"
    rf"|\b(?:(?:{_DOWN_SUBJECTS})\s+(?:is\s+|are\s+)?|(?:is|are|went|gone|completely|totally)\s+)down\b",
    re.IGNORECASE,
)
WEAK_PRIORITY_RE = re.compile(r"\b(asap|high\s+priority|important|down)\b", re.IGNORECASE)
# "not urgent", "non-critical", "isn't an emergency", "no outage" must not escalate.
NEGATION_RE = re.compile(
    r"(?:\bnot|\bno|\bnon|n't)[\s-]+(?:(?:an?|very|really|too|that|super|so)\s+)?$", re.IGNORECASE
)
PRIORITY_BODY_SCAN_CHARS = 2000

# Colours/markers for agent-facing mail.
PRIORITY_STYLE = {
    TicketPriority.LOW: ("#4b5563", "#f3f4f6"),
    TicketPriority.MEDIUM: ("#1d4ed8", "#dbeafe"),
    TicketPriority.HIGH: ("#b45309", "#fef3c7"),
    TicketPriority.URGENT: ("#ffffff", "#dc2626"),
}
# Standard importance headers so mail clients flag high/urgent alerts too.
PRIORITY_HEADERS = {
    TicketPriority.URGENT: {"X-Priority": "1 (Highest)", "Importance": "high"},
    TicketPriority.HIGH: {"X-Priority": "2 (High)", "Importance": "high"},
}


# =========================================================================== errors


class EmailError(Exception):
    """Base class for mail transport failures."""


class TransientEmailError(EmailError):
    """Network/server hiccup that exhausted its retries; safe to try again later."""


class PermanentEmailError(EmailError):
    """Will not succeed on retry (bad credentials, rejected recipient, TLS failure...)."""


def is_transient(exc: BaseException) -> bool:
    """Classify an exception from imaplib/smtplib/socket as retryable or not.

    Order matters: smtplib exceptions subclass OSError, and IMAP4.abort subclasses
    IMAP4.error, so the specific cases are checked before the broad ones.
    """
    if isinstance(exc, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError)):
        return True
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return False
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500  # 4xx = try again later, 5xx = give up
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return all(400 <= code < 500 for code, _ in exc.recipients.values())
    if isinstance(exc, smtplib.SMTPException):
        return False
    if isinstance(exc, imaplib.IMAP4.abort):  # connection dropped / server BYE
        return True
    if isinstance(exc, imaplib.IMAP4.error):  # protocol NO/BAD, e.g. login failed
        return False
    if isinstance(exc, ssl.SSLCertVerificationError):
        return False
    return isinstance(exc, (OSError, EOFError))  # timeouts, resets, DNS, TLS EOF


# =========================================================================== data


@dataclass(slots=True)
class InboundAttachment:
    filename: str
    content_type: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(slots=True)
class InboundEmail:
    uid: str | None
    message_id: str
    in_reply_to: str | None
    references: list[str]
    subject: str
    from_address: str
    from_name: str | None
    to: list[str]
    cc: list[str]
    date: datetime | None
    text_body: str
    html_body: str | None  # already sanitized
    attachments: list[InboundAttachment] = field(default_factory=list)
    skipped_attachments: list[str] = field(default_factory=list)
    is_auto_generated: bool = False
    # Priority to use if this email opens a new ticket (see assess_priority).
    priority: TicketPriority = TicketPriority.MEDIUM
    priority_reasons: list[str] = field(default_factory=list)

    @property
    def ticket_ref(self) -> int | None:
        match = TICKET_REF_RE.search(self.subject)
        return int(match.group(1)) if match else None

    @property
    def is_noreply(self) -> bool:
        """A no-reply sender: we may open a ticket, but never send it an acknowledgement."""
        return bool(NOREPLY_RE.match(self.from_address.split("@", 1)[0]))

    @property
    def legacy_ref(self) -> str | None:
        """HESK tracking ID from a reply to an email HESK sent. HESK strips spaces from
        the subject before matching, so do the same."""
        match = HESK_REF_RE.search(self.subject.replace(" ", ""))
        return match.group(1) if match else None

    @property
    def thread_message_ids(self) -> list[str]:
        """Candidate parent Message-IDs, most specific first, de-duplicated."""
        ids = ([self.in_reply_to] if self.in_reply_to else []) + list(reversed(self.references))
        return list(dict.fromkeys(ids))

    @property
    def reply_text(self) -> str:
        """Body with quoted history removed; use for comments appended to a thread."""
        return strip_quoted_reply(self.text_body)

    @property
    def clean_subject(self) -> str:
        return clean_subject(self.subject)


@dataclass(slots=True)
class PollResult:
    fetched: int = 0
    processed: int = 0
    failed: int = 0
    quarantined: int = 0


@dataclass(slots=True)
class SendReport:
    sent: list[str] = field(default_factory=list)  # Message-IDs
    failed: dict[str, str] = field(default_factory=dict)  # Message-ID -> error
    transient: set[str] = field(default_factory=set)  # failed IDs worth retrying later

    @property
    def ok(self) -> bool:
        return not self.failed


# =========================================================================== text helpers


def sanitize_html(raw_html: str) -> str:
    return nh3.clean(
        raw_html,
        tags=ALLOWED_HTML_TAGS,
        clean_content_tags=STRIPPED_CONTENT_TAGS,
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer nofollow",
    )


def html_to_text(raw_html: str) -> str:
    # Turn block boundaries into newlines before stripping every tag.
    marked = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6]|blockquote)>", "\n", raw_html)
    text = html.unescape(nh3.clean(marked, tags=set(), clean_content_tags=STRIPPED_CONTENT_TAGS))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def strip_quoted_reply(text: str) -> str:
    cut = len(text)
    for pattern in QUOTE_HEADER_RES:
        if match := pattern.search(text):
            cut = min(cut, match.start())
    lines = text[:cut].rstrip().splitlines()
    while lines and (lines[-1].startswith(">") or not lines[-1].strip()):
        lines.pop()
    stripped = "\n".join(lines).strip()
    return stripped or text.strip()  # never throw away a message entirely


def clean_subject(subject: str) -> str:
    """Drop Re:/Fwd: prefixes and any ticket tag so we can re-apply them canonically."""
    subject = TICKET_REF_RE.sub("", subject)
    subject = SUBJECT_PREFIX_RE.sub("", subject)
    return re.sub(r"\s+", " ", subject).strip() or "(no subject)"


@dataclass(slots=True)
class PriorityAssessment:
    priority: TicketPriority
    reasons: list[str]

    @property
    def escalated(self) -> bool:
        return self.priority.rank > TicketPriority.MEDIUM.rank


def _find_keyword(pattern: re.Pattern[str], text: str) -> str | None:
    """First non-negated keyword match in `text`."""
    for match in pattern.finditer(text):
        if not NEGATION_RE.search(text[max(0, match.start() - 25) : match.start()]):
            return match.group(0)
    return None


def _header_priority(importance: str, x_priority: str, priority: str) -> TicketPriority | None:
    """Priority the *sender* explicitly set in their mail client, if any."""
    importance, priority = importance.strip().lower(), priority.strip().lower()
    level = re.match(r"\s*(\d)", x_priority)
    if importance == "high" or priority == "urgent" or (level and level.group(1) in "12"):
        return TicketPriority.HIGH
    if importance == "low" or priority == "non-urgent" or (level and level.group(1) in "45"):
        return TicketPriority.LOW
    return None


def assess_priority(
    subject: str,
    body: str,
    *,
    importance: str = "",
    x_priority: str = "",
    priority_header: str = "",
    keywords_enabled: bool = True,
) -> PriorityAssessment:
    """Initial priority for a new ticket: MEDIUM, unless the sender flagged the
    message or urgency keywords escalate it.

      strong keyword in subject (URGENT, EMERGENCY, CRITICAL, OUTAGE, "server down")  -> URGENT
      strong keyword in body, weak keyword in subject (ASAP, DOWN, IMPORTANT),
      or sender-set high importance                                                   -> HIGH
      sender-set low importance with no keywords                                      -> LOW
    """
    subject = clean_subject(subject)
    body = strip_quoted_reply(body)[:PRIORITY_BODY_SCAN_CHARS]
    candidates: list[tuple[TicketPriority, str]] = []

    if keywords_enabled:
        if word := _find_keyword(STRONG_PRIORITY_RE, subject):
            candidates.append((TicketPriority.URGENT, f'keyword "{word}" in subject'))
        if word := _find_keyword(STRONG_PRIORITY_RE, body):
            candidates.append((TicketPriority.HIGH, f'keyword "{word}" in body'))
        if word := _find_keyword(WEAK_PRIORITY_RE, subject):
            candidates.append((TicketPriority.HIGH, f'keyword "{word}" in subject'))

    header = _header_priority(importance, x_priority, priority_header)
    if header == TicketPriority.HIGH:
        candidates.append((TicketPriority.HIGH, "sender marked the email as high importance"))

    if candidates:
        top = TicketPriority.highest(*(p for p, _ in candidates))
        return PriorityAssessment(top, [reason for p, reason in candidates if p == top])
    if header == TicketPriority.LOW:
        return PriorityAssessment(TicketPriority.LOW, ["sender marked the email as low importance"])
    return PriorityAssessment(TicketPriority.MEDIUM, [])


def _one_line(value: str) -> str:
    """Guard against header injection from user-controlled strings."""
    return re.sub(r"[\r\n]+", " ", value).strip()


def safe_filename(name: str | None, fallback: str) -> str:
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^\w.\- ()]+", "_", name).strip(" .")
    return name[:200] or fallback


# =========================================================================== service


class EmailService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._ssl_context = ssl.create_default_context()
        self._msgid_domain = settings.smtp_from_address.rsplit("@", 1)[-1]
        # (uidvalidity, uid) -> consecutive handler failures; survives reconnects.
        self._failures: dict[tuple[str, str], int] = {}

    # ------------------------------------------------------------------ retry

    def _retry(self, op: str, fn: Callable[[], T]) -> T:
        s = self.settings
        for attempt in range(s.email_max_retries + 1):
            try:
                return fn()
            except Exception as exc:
                if not is_transient(exc):
                    raise PermanentEmailError(f"{op}: {exc!r}") from exc
                if attempt >= s.email_max_retries:
                    raise TransientEmailError(
                        f"{op}: gave up after {attempt + 1} attempts: {exc!r}"
                    ) from exc
                delay = min(s.email_retry_max_delay, s.email_retry_base_delay * 2**attempt)
                delay *= random.uniform(0.5, 1.0)  # jitter avoids thundering herds
                log.warning("%s failed (%r); retry %d in %.1fs", op, exc, attempt + 1, delay)
                time.sleep(delay)
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------ IMAP

    def _imap_connect(self) -> imaplib.IMAP4:
        s = self.settings
        if s.imap_use_ssl:
            conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(
                s.imap_host, s.imap_port, ssl_context=self._ssl_context, timeout=s.imap_timeout
            )
        else:
            conn = imaplib.IMAP4(s.imap_host, s.imap_port, timeout=s.imap_timeout)
            if s.imap_use_starttls:
                conn.starttls(ssl_context=self._ssl_context)
        try:
            conn.login(s.imap_user, s.imap_password)
            status, _ = conn.select(s.imap_mailbox)
            if status != "OK":
                raise imaplib.IMAP4.error(f"cannot select mailbox {s.imap_mailbox!r}")
        except BaseException:
            _quietly(conn.shutdown)
            raise
        return conn

    @contextmanager
    def imap_session(self) -> Iterator[imaplib.IMAP4]:
        # No retry here: poll_inbox() retries the whole session, which also covers
        # connections that drop mid-batch.
        conn = self._imap_connect()
        try:
            yield conn
        finally:
            _quietly(conn.close)
            _quietly(conn.logout)

    def poll_inbox(self, handler: Callable[[InboundEmail], None]) -> PollResult:
        """Process unread mail once. Transport failures are retried with backoff.

        A message is flagged \\Seen only after `handler` returns, so a crash never loses
        mail. Messages whose handler keeps failing are quarantined (\\Seen \\Flagged)
        after IMAP_MAX_FAILURES attempts so one poison message can't block the queue.
        Re-delivery after a crash between handler and flag update is expected; the
        handler must be idempotent (the ingestor de-duplicates on Message-ID).
        """
        return self._retry("IMAP poll", lambda: self._poll_once(handler))

    def _poll_once(self, handler: Callable[[InboundEmail], None]) -> PollResult:
        result = PollResult()
        with self.imap_session() as conn:
            uidvalidity = _untagged(conn, "UIDVALIDITY") or "0"
            status, data = conn.uid("SEARCH", None, "UNSEEN")
            _expect_ok(status, data, "UID SEARCH")
            uids = data[0].decode().split()[: self.settings.imap_batch_size]
            if uids:
                log.info("IMAP: %d unread message(s) to process", len(uids))

            for uid in uids:
                # BODY.PEEK leaves the message unread until we have handled it.
                status, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                _expect_ok(status, data, f"UID FETCH {uid}")
                raw = next((p[1] for p in data if isinstance(p, tuple)), None)
                if raw is None:
                    continue  # expunged by another client meanwhile
                result.fetched += 1
                key = (uidvalidity, uid)

                try:
                    handler(self.parse_message(raw, uid=uid))
                except Exception:
                    log.exception("IMAP: handler failed for UID %s", uid)
                    result.failed += 1
                    self._failures[key] = self._failures.get(key, 0) + 1
                    if self._failures[key] >= self.settings.imap_max_failures:
                        conn.uid("STORE", uid, "+FLAGS", QUARANTINE_FLAGS)
                        self._failures.pop(key, None)
                        result.quarantined += 1
                        log.error("IMAP: UID %s quarantined (flagged) after repeated failures", uid)
                    continue

                conn.uid("STORE", uid, "+FLAGS", r"(\Seen)")
                self._failures.pop(key, None)
                result.processed += 1
        return result

    # ------------------------------------------------------------------ parsing

    def parse_message(self, raw: bytes, uid: str | None = None) -> InboundEmail:
        msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)  # type: ignore[assignment]

        message_id = _first_msgid(_header(msg, "Message-ID"))
        if not message_id:  # rare, but we need a stable key for idempotency
            message_id = f"<{hashlib.sha256(raw).hexdigest()}@generated.invalid>"

        from_name, from_address = parseaddr(_header(msg, "From"))
        date = None
        try:
            date = msg["Date"].datetime if msg["Date"] else None
        except (AttributeError, TypeError, ValueError):
            pass

        text_part = msg.get_body(preferencelist=("plain",))
        html_part = msg.get_body(preferencelist=("html",))
        raw_html = _part_text(html_part) if html_part is not None else None
        html_body = sanitize_html(raw_html) if raw_html else None
        if text_part is not None:
            text_body = _part_text(text_part).strip()
        else:
            text_body = html_to_text(raw_html) if raw_html else ""

        attachments, skipped = self._extract_attachments(msg, {id(text_part), id(html_part)})
        subject = _one_line(_header(msg, "Subject"))
        assessment = assess_priority(
            subject,
            text_body,
            importance=_header(msg, "Importance"),
            x_priority=_header(msg, "X-Priority"),
            priority_header=_header(msg, "Priority"),
            keywords_enabled=self.settings.priority_escalation_enabled,
        )

        return InboundEmail(
            uid=uid,
            message_id=message_id,
            in_reply_to=_first_msgid(_header(msg, "In-Reply-To")),
            references=MSGID_RE.findall(_header(msg, "References")),
            subject=subject,
            from_address=from_address.strip().lower(),
            from_name=from_name.strip() or None,
            to=[a.lower() for _, a in getaddresses(msg.get_all("To", [])) if a],
            cc=[a.lower() for _, a in getaddresses(msg.get_all("Cc", [])) if a],
            date=date,
            text_body=text_body,
            html_body=html_body,
            attachments=attachments,
            skipped_attachments=skipped,
            is_auto_generated=_is_auto_generated(
                msg, own_address=self.settings.smtp_from_address, from_address=from_address, subject=subject,
            ),
            priority=assessment.priority,
            priority_reasons=assessment.reasons,
        )

    def _extract_attachments(
        self, msg: EmailMessage, body_part_ids: set[int]
    ) -> tuple[list[InboundAttachment], list[str]]:
        attachments: list[InboundAttachment] = []
        skipped: list[str] = []
        for index, part in enumerate(_leaf_parts(msg)):
            if id(part) in body_part_ids:
                continue
            ctype = part.get_content_type()
            filename = part.get_filename()
            is_attachment = part.get_content_disposition() == "attachment" or filename
            if not is_attachment and ctype != "message/rfc822":
                continue  # e.g. alternative body parts we didn't pick
            try:
                if ctype == "message/rfc822":
                    data = part.get_payload(0).as_bytes()
                    default_name = f"forwarded-{index}.eml"
                else:
                    data = part.get_payload(decode=True) or b""
                    default_name = f"attachment-{index}"
            except Exception:
                log.warning("Could not decode attachment %r", filename, exc_info=True)
                skipped.append(filename or f"part-{index}")
                continue
            name = safe_filename(filename, default_name)
            if len(data) > self.settings.attachment_max_bytes:
                skipped.append(f"{name} (too large: {len(data)} bytes)")
                continue
            attachments.append(InboundAttachment(name, ctype, data))
        return attachments, skipped

    # ------------------------------------------------------------------ SMTP

    def _smtp_connect(self) -> smtplib.SMTP:
        s = self.settings
        if s.smtp_use_ssl:
            conn: smtplib.SMTP = smtplib.SMTP_SSL(
                s.smtp_host, s.smtp_port, timeout=s.smtp_timeout, context=self._ssl_context
            )
        else:
            conn = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout)
        try:
            conn.ehlo()
            if s.smtp_use_starttls:
                conn.starttls(context=self._ssl_context)
                conn.ehlo()
            if s.smtp_user:
                conn.login(s.smtp_user, s.smtp_password or "")
        except BaseException:
            _quietly(conn.close)
            raise
        return conn

    def send_messages(self, messages: Sequence[EmailMessage]) -> SendReport:
        """Send over one pooled connection, reconnecting with backoff when it drops.

        Per-message permanent rejections (e.g. unknown recipient) are recorded and
        skipped; transient failures reconnect and resume from the failed message.
        """
        s = self.settings
        report = SendReport()
        pending = list(messages)
        attempt = 0
        while pending:
            conn = None
            try:
                conn = self._smtp_connect()
                while pending:
                    msg = pending[0]
                    try:
                        conn.send_message(msg)
                    except Exception as exc:
                        if is_transient(exc):
                            raise
                        report.failed[msg["Message-ID"]] = repr(exc)
                        log.error("SMTP: permanent failure for %s: %r", msg["To"], exc)
                    else:
                        report.sent.append(msg["Message-ID"])
                        attempt = 0  # progress made; reset backoff budget
                    pending.pop(0)
            except Exception as exc:
                if not is_transient(exc) or attempt >= s.email_max_retries:
                    for msg in pending:
                        report.failed[msg["Message-ID"]] = repr(exc)
                        if is_transient(exc):
                            report.transient.add(msg["Message-ID"])
                    log.error("SMTP: giving up on %d message(s): %r", len(pending), exc)
                    break
                delay = min(s.email_retry_max_delay, s.email_retry_base_delay * 2**attempt)
                delay *= random.uniform(0.5, 1.0)
                attempt += 1
                log.warning("SMTP: %r; reconnect attempt %d in %.1fs", exc, attempt, delay)
                time.sleep(delay)
            finally:
                if conn is not None:
                    _quietly(conn.quit)
        return report

    def send(self, message: EmailMessage) -> str:
        """Send one message; returns its Message-ID.

        Raises TransientEmailError when the server was unreachable or deferred us
        (worth retrying later) and PermanentEmailError when it was rejected outright.
        """
        report = self.send_messages([message])
        mid = message["Message-ID"]
        if not report.ok:
            error_cls = TransientEmailError if mid in report.transient else PermanentEmailError
            raise error_cls(report.failed[mid])
        return mid

    def compose(
        self,
        *,
        to: str | Iterable[str],
        subject: str,
        text: str,
        html_body: str | None = None,
        in_reply_to: str | None = None,
        references: Sequence[str] = (),
        automated: bool = False,
        extra_headers: dict[str, str] | None = None,
        message_id: str | None = None,
    ) -> EmailMessage:
        s = self.settings
        msg = EmailMessage()
        msg["From"] = formataddr((_one_line(s.smtp_from_name), s.smtp_from_address))
        msg["To"] = ", ".join([to] if isinstance(to, str) else to)
        msg["Subject"] = _one_line(subject)
        msg["Date"] = formatdate(usegmt=True)
        msg["Message-ID"] = message_id or make_msgid(domain=self._msgid_domain)
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        if references:
            refs = list(dict.fromkeys(references))
            if len(refs) > MAX_REFERENCES:  # RFC 5322: keep the root + most recent
                refs = refs[:1] + refs[-(MAX_REFERENCES - 1) :]
            msg["References"] = " ".join(refs)
        # Stamped on everything we send: if it ever comes back to the support mailbox
        # (bounced, forwarded by a rule), the ingestion loop guard recognises it.
        msg["X-Loop"] = s.smtp_from_address
        if automated:
            # Tells well-behaved auto-responders (OOO etc.) not to reply -> no mail loops.
            msg["Auto-Submitted"] = "auto-generated"
            msg["X-Auto-Response-Suppress"] = "All"
        for name, value in (extra_headers or {}).items():
            msg[name] = _one_line(value)
        msg.set_content(text)
        if html_body:
            msg.add_alternative(html_body, subtype="html")
        return msg

    # ------------------------------------------------------------------ ticket mail

    def ticket_url(self, ticket: Ticket) -> str:
        return f"{self.settings.app_base_url.rstrip('/')}/#/tickets/{ticket.id}"

    def portal_url(self, ticket: Ticket | None = None) -> str:
        base = f"{self.settings.app_base_url.rstrip('/')}/portal/"
        return f"{base}#/tickets/{ticket.id}" if ticket is not None else base

    def _portal_line(self, ticket: Ticket) -> str:
        if not self.settings.portal_enabled:
            return ""
        return f"\nYou can also follow this ticket online: {self.portal_url(ticket)}\n"

    def send_portal_link(self, email: str, link: str, site_name: str, minutes: int) -> None:
        """One-time sign-in link for the customer portal. Raises EmailError."""
        msg = self.compose(
            to=email,
            subject=f"Your sign-in link for {site_name}",
            text=(
                f"Hello,\n\nUse this link to sign in to {site_name}:\n\n{link}\n\n"
                f"The link works once and expires in {minutes} minutes.\n"
                "If you didn't ask for it, you can ignore this email; nobody can sign in without it.\n"
            ),
            automated=True,
        )
        self.send(msg)

    def build_customer_reply(
        self, ticket: Ticket, comment: TicketComment, agent: User | None = None
    ) -> EmailMessage:
        """Compose an agent's public reply, threaded into the customer's conversation.

        Stamps comment.message_id / in_reply_to / email_metadata (the caller commits).
        A comment that already has a Message-ID keeps it, so a retried send is the
        *same* message to the recipient's server and threading stays intact.
        `ticket.comments` is read to build References, so the ticket must be attached
        to an open session.
        """
        thread = [ticket.message_id] + [c.message_id for c in ticket.comments if c is not comment]
        thread = [mid for mid in thread if mid and mid != comment.message_id]
        last_inbound = next(
            (c.message_id for c in reversed(ticket.comments) if c.is_from_customer and c.message_id),
            ticket.message_id,
        )
        # Agents with their own signature already have it in the reply text (or chose
        # to leave it off); everyone else gets a simple name line.
        signature = (
            f"\n\n-- \n{agent.full_name}\n{self.settings.smtp_from_name}"
            if agent and not agent.signature else ""
        )
        text = (
            f"{REPLY_MARKER}\n\n"
            f"{comment.body.strip()}{signature}\n\n"
            f"Ticket reference: {ticket.subject_tag} - please keep it in the subject line."
            f"{self._portal_line(ticket)}"
        )
        msg = self.compose(
            to=formataddr((ticket.requester_name or "", ticket.requester_email)),
            subject=f"Re: {ticket.subject_tag} {clean_subject(ticket.subject)}",
            text=text,
            in_reply_to=last_inbound,
            references=thread,
            extra_headers={"X-Ticket-ID": ticket.tracking_code},
            message_id=comment.message_id,
        )
        comment.message_id = msg["Message-ID"]
        comment.in_reply_to = last_inbound
        comment.email_metadata = {"to": msg["To"], "subject": msg["Subject"], "references": thread}
        return msg

    def send_reply_to_customer(
        self, ticket: Ticket, comment: TicketComment, agent: User | None = None
    ) -> bool:
        """Build + send in one step, recording the outcome on the comment (no retry
        scheduling; the API uses app.notifications.deliver_reply for that)."""
        msg = self.build_customer_reply(ticket, comment, agent)
        try:
            self.send(msg)
        except EmailError as exc:
            comment.delivery_status = DeliveryStatus.FAILED
            comment.delivery_error = str(exc)[:2000]
            log.error("Reply for %s not delivered: %s", ticket.tracking_code, exc)
            return False
        comment.delivery_status = DeliveryStatus.SENT
        comment.delivery_error = None
        comment.delivered_at = datetime.now(UTC)
        return True

    def send_new_ticket_ack(self, ticket: Ticket) -> str | None:
        """Auto-acknowledge a new email ticket so the customer learns the tracking ID."""
        msg = self.compose(
            to=ticket.requester_email,
            subject=f"{ticket.subject_tag} {clean_subject(ticket.subject)}",
            text=(
                f"{REPLY_MARKER}\n\nHello{(' ' + ticket.requester_name) if ticket.requester_name else ''},\n\n"
                f"We received your request and opened ticket {ticket.tracking_code}.\n"
                "An agent will get back to you shortly. Simply reply to this email to add "
                "more information.\n"
                f"{self._portal_line(ticket)}"
            ),
            in_reply_to=ticket.message_id,
            references=[ticket.message_id] if ticket.message_id else [],
            automated=True,
            extra_headers={"X-Ticket-ID": ticket.tracking_code},
        )
        try:
            return self.send(msg)
        except EmailError as exc:
            log.error("Ack for %s not delivered: %s", ticket.tracking_code, exc)
            return None

    def broadcast_new_ticket(
        self, ticket: Ticket, agents: Iterable[User], priority_reasons: Sequence[str] = ()
    ) -> SendReport:
        """Alert every agent about a newly created ticket (one message per agent).

        The priority leads the subject (`[URGENT] New Ticket Created: ...`) and heads
        the body; high/urgent alerts also carry X-Priority/Importance headers.
        """
        priority = ticket.priority
        tag = priority.label.upper()
        subject = f"[{tag}] New Ticket Created: {ticket.subject_tag} {clean_subject(ticket.subject)}"
        requester = formataddr((ticket.requester_name or "", ticket.requester_email))
        url = self.ticket_url(ticket)
        excerpt = (ticket.description or "").strip()
        if len(excerpt) > 1500:
            excerpt = excerpt[:1500].rstrip() + " [...]"
        why = f"Auto-escalated: {'; '.join(priority_reasons)}" if priority_reasons else ""

        banner = f"PRIORITY: {tag}"
        rule = "=" * max(len(banner), len(why)) if why else "=" * len(banner)
        text = (
            f"{rule}\n{banner}\n" + (f"{why}\n" if why else "") + f"{rule}\n\n"
            f"A new ticket was created.\n\n"
            f"Ticket:   {ticket.tracking_code}\n"
            f"Priority: {priority.label}\n"
            f"Subject:  {ticket.subject}\n"
            f"From:     {requester}\n"
            f"Open:     {url}\n\n"
            f"{excerpt}\n"
        )

        fg, bg = PRIORITY_STYLE[priority]
        e = html.escape
        html_body = f"""\
<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#111827;max-width:640px">
  <div style="background:{bg};color:{fg};padding:12px 16px;border-radius:6px;font-weight:bold;font-size:18px;letter-spacing:.5px">
    PRIORITY: {e(tag)}
    {f'<div style="font-size:12px;font-weight:normal;margin-top:4px">{e(why)}</div>' if why else ''}
  </div>
  <h2 style="font-size:18px;margin:16px 0 8px">New Ticket Created: {e(ticket.tracking_code)}</h2>
  <table style="border-collapse:collapse;font-size:14px">
    <tr><td style="padding:2px 12px 2px 0;color:#6b7280">Subject</td><td>{e(ticket.subject)}</td></tr>
    <tr><td style="padding:2px 12px 2px 0;color:#6b7280">From</td><td>{e(requester)}</td></tr>
    <tr><td style="padding:2px 12px 2px 0;color:#6b7280">Priority</td>
        <td><span style="background:{bg};color:{fg};padding:1px 8px;border-radius:10px;font-weight:bold">{e(priority.label)}</span></td></tr>
  </table>
  <p><a href="{e(url, quote=True)}" style="display:inline-block;background:#111827;color:#ffffff;padding:8px 14px;border-radius:4px;text-decoration:none">Open ticket</a></p>
  <pre style="white-space:pre-wrap;font-family:inherit;background:#f9fafb;border-left:3px solid #d1d5db;padding:8px 12px">{e(excerpt)}</pre>
</div>"""

        messages = [
            self.compose(
                to=str(Address(display_name=agent.full_name, addr_spec=agent.email)),
                subject=subject,
                text=text,
                html_body=html_body,
                automated=True,
                extra_headers={
                    "X-Ticket-ID": ticket.tracking_code,
                    "X-Ticket-Priority": priority.value,
                    **PRIORITY_HEADERS.get(priority, {}),
                },
            )
            for agent in agents
        ]
        if not messages:
            log.warning("No active agents to notify about %s", ticket.tracking_code)
            return SendReport()
        report = self.send_messages(messages)
        log.info(
            "Broadcast %s (%s): %d sent, %d failed",
            ticket.tracking_code, priority.value, len(report.sent), len(report.failed),
        )
        return report

    def alert_customer_reply(
        self, ticket: Ticket, comment: TicketComment, recipients: Iterable[User], *, reopened: bool = False
    ) -> SendReport:
        """Tell staff a customer is waiting: `[HIGH] Customer replied: [TICKET-n] ...`."""
        priority = ticket.priority
        tag = priority.label.upper()
        who = formataddr((comment.author_name or ticket.requester_name or "", comment.author_email or ticket.requester_email))
        via = "the customer portal" if comment.source.value == "web" else "email"
        excerpt = (comment.body or "").strip()
        if len(excerpt) > 1500:
            excerpt = excerpt[:1500].rstrip() + " [...]"
        headline = "Customer replied (ticket reopened)" if reopened else "Customer replied"
        assignee = ticket.assignee.full_name if ticket.assignee else "Unassigned"
        text = (
            f"{headline}\n\n"
            f"Ticket:   {ticket.tracking_code}\n"
            f"Subject:  {ticket.subject}\n"
            f"From:     {who} (via {via})\n"
            f"Priority: {priority.label}\n"
            f"Status:   {ticket.status.value}\n"
            f"Assigned: {assignee}\n"
            f"Open:     {self.ticket_url(ticket)}\n\n"
            f"{excerpt}\n\n"
            "Reply from the dashboard so the customer gets your answer. (Replying to this\n"
            "email adds an internal note instead.)\n"
        )
        messages = [
            self.compose(
                to=str(Address(display_name=user.full_name, addr_spec=user.email)),
                subject=f"[{tag}] {headline}: {ticket.subject_tag} {clean_subject(ticket.subject)}",
                text=text,
                automated=True,
                extra_headers={
                    "X-Ticket-ID": ticket.tracking_code,
                    "X-Ticket-Priority": priority.value,
                    **PRIORITY_HEADERS.get(priority, {}),
                },
            )
            for user in recipients
        ]
        report = self.send_messages(messages)
        log.info("Reply alert %s: %d sent, %d failed", ticket.tracking_code, len(report.sent), len(report.failed))
        return report

    def notify_assignment(
        self, ticket: Ticket, assignee: User, assigned_by: User | None = None
    ) -> str | None:
        """Tell an agent a ticket was (re)assigned to them. Returns the Message-ID."""
        by = f" by {assigned_by.full_name}" if assigned_by else ""
        msg = self.compose(
            to=str(Address(display_name=assignee.full_name, addr_spec=assignee.email)),
            subject=(
                f"[{ticket.priority.label.upper()}] Assigned to you: "
                f"{ticket.subject_tag} {clean_subject(ticket.subject)}"
            ),
            text=(
                f"Hi {assignee.full_name},\n\n"
                f"Ticket {ticket.tracking_code} has been assigned to you{by}.\n\n"
                f"Subject:  {ticket.subject}\n"
                f"Priority: {ticket.priority.label.upper()}\n"
                f"Status:   {ticket.status.value}\n"
                f"Open:     {self.ticket_url(ticket)}\n"
            ),
            automated=True,
            extra_headers={
                "X-Ticket-ID": ticket.tracking_code,
                "X-Ticket-Priority": ticket.priority.value,
                **PRIORITY_HEADERS.get(ticket.priority, {}),
            },
        )
        try:
            return self.send(msg)
        except EmailError as exc:
            log.error("Assignment notice for %s not delivered: %s", ticket.tracking_code, exc)
            return None


# =========================================================================== internals


def _quietly(fn: Callable[[], object]) -> None:
    try:
        fn()
    except Exception:
        pass


def _expect_ok(status: str, data: list, op: str) -> None:
    if status != "OK":
        raise imaplib.IMAP4.error(f"{op} failed: {status} {data!r}")


def _untagged(conn: imaplib.IMAP4, name: str) -> str | None:
    _, data = conn.response(name)
    value = data[0] if data else None
    return value.decode() if isinstance(value, bytes) else value


def _header(msg: EmailMessage, name: str) -> str:
    """Header as str; malformed encoded-words degrade to raw text instead of raising."""
    try:
        return str(msg.get(name) or "")
    except Exception:
        return next((str(v) for k, v in msg.raw_items() if k.lower() == name.lower()), "")


def _first_msgid(value: str) -> str | None:
    match = MSGID_RE.search(value or "")
    return match.group(0) if match else None


def _part_text(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError):  # unknown/lying charset
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace")


def _leaf_parts(part: EmailMessage) -> Iterator[EmailMessage]:
    """Walk MIME leaves, treating attached emails as opaque (not descending into them)."""
    if part.get_content_type() == "message/rfc822":
        yield part
    elif part.is_multipart():
        for sub in part.iter_parts():
            yield from _leaf_parts(sub)  # type: ignore[arg-type]
    else:
        yield part


# Subjects used by auto-responders and bounce messages (English plus the common
# European variants), matched at the start after Re:/Fwd: prefixes are removed.
AUTO_REPLY_SUBJECT_RE = re.compile(
    r"^\s*(?:(?:re|fwd?|aw|sv|antw)\s*:\s*)*("
    r"auto(?:matic|matische?)?[\s-]*(?:reply|response|antwort|answer)"
    r"|auto:\s*(?:re|aw|sv)\s*:"
    r"|autoreply|autoresponse|out of (?:the )?office|ooo\b|on (?:annual )?leave"
    r"|abwesenheit|r[ée]ponse automatique|absence|respuesta autom[áa]tica|fuera de la oficina"
    r"|risposta automatica|automatisch antwoord|afwezig|autosvar|fr[åa]nvaro|poza biurem"
    r"|delivery status notification|undeliver(?:able|ed)|mail delivery (?:failed|subsystem)"
    r"|returned mail|failure notice|delivery failure|message not delivered|non[- ]?delivery"
    r")",
    re.IGNORECASE,
)
# Mailboxes that only ever send automated mail (bounces, system notices).
BOUNCE_SENDERS = {"mailer-daemon", "postmaster", "mail-daemon", "bounce", "bounces"}
NOREPLY_RE = re.compile(r"^(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply)", re.IGNORECASE)


def _is_auto_generated(msg: EmailMessage, *, own_address: str = "", from_address: str = "", subject: str = "") -> bool:
    """Detect bounces, out-of-office replies and other automatic mail, so it never
    creates tickets, reopens them or triggers alerts (which is what keeps an
    auto-responder and the help desk from answering each other forever)."""
    auto_submitted = _header(msg, "Auto-Submitted").strip().lower()
    if auto_submitted and auto_submitted != "no":                      # RFC 3834
        return True
    if _header(msg, "Precedence").strip().lower() in {"bulk", "junk", "list", "auto_reply"}:
        return True
    if msg.get("X-Autoreply") or msg.get("X-Autorespond") or msg.get("X-Autoresponder"):
        return True
    suppress = _header(msg, "X-Auto-Response-Suppress").lower()        # Exchange/Outlook
    if "all" in suppress or "oof" in suppress:
        return True
    if own_address and own_address.lower() in _header(msg, "X-Loop").lower():
        return True                                                     # our own mail, bounced back
    if _header(msg, "Return-Path").strip() == "<>":                   # DSN / bounce
        return True
    if msg.get_content_type() == "multipart/report":
        return True
    if from_address.split("@", 1)[0].lower() in BOUNCE_SENDERS:
        return True
    return bool(AUTO_REPLY_SUBJECT_RE.search(subject or ""))
