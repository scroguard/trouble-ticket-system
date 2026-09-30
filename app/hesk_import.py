"""Import ticket history from HESK (https://www.hesk.com).

    docker compose run --rm -v /path/to/hesk/attachments:/hesk-attachments:ro web \\
        python -m app.hesk_import --hesk-db mysql://user:pass@host:3306/hesk \\
        --attachments-dir /hesk-attachments --dry-run

Reads the HESK MySQL/MariaDB database (read-only) and its attachments folder, and
creates tickets, customer replies, staff replies, internal notes and attachments with
their original timestamps. Nothing is emailed.

Safe to run repeatedly: tickets are matched on the HESK tracking ID and replies/notes
on their HESK ids, so a second run only adds what is new (e.g. a final catch-up at
switchover). Supports both HESK data layouts:
  * up to 3.4: the customer's name/email are columns on the ticket;
  * 3.5+: customers live in `customers`, linked via `ticket_to_customer` (REQUESTER /
    FOLLOWER), and customer replies point to a customer id. Installs upgraded to 3.5
    also keep the old values in `tickets.u_name` / `u_email`, used as a fallback.
Columns and tables that a given HESK version lacks are skipped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import sys
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pymysql
import pymysql.cursors
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.email_service import html_to_text, sanitize_html
from app.models import (
    CLOSED_STATUSES,
    MessageSource,
    Ticket,
    TicketAttachment,
    TicketComment,
    TicketPriority,
    TicketStatus,
    User,
    UserRole,
)
from app.security import hash_password
from app.storage import write_blob

log = logging.getLogger("app.hesk_import")

CHUNK = 200
IMPORT_AUTHOR = "HESK import"
NO_EMAIL_DOMAIN = "hesk-import.invalid"  # RFC 2606: guaranteed never to receive mail

# HESK built-in statuses (inc/statuses.inc.php); custom ones start at 6.
HESK_STATUS_NAMES = {0: "New", 1: "Waiting reply", 2: "Replied", 3: "Resolved", 4: "In Progress", 5: "On Hold"}
STATUS_MAP = {
    0: TicketStatus.NEW,
    1: TicketStatus.OPEN,           # customer replied, waiting on staff
    2: TicketStatus.PENDING,        # staff replied, waiting on customer
    3: TicketStatus.RESOLVED,
    4: TicketStatus.IN_PROGRESS,
    5: TicketStatus.PENDING,        # on hold
}
CUSTOM_STATUS_DEFAULT = TicketStatus.OPEN
PRIORITY_MAP = {"0": TicketPriority.URGENT, "1": TicketPriority.HIGH, "2": TicketPriority.MEDIUM, "3": TicketPriority.LOW}
PRIORITY_NAMES = {"0": "Critical", "1": "High", "2": "Medium", "3": "Low"}
OPENED_VIA = {-1: "email (piping)", -2: "email (POP3)", -3: "email (IMAP)", 0: "web form"}
EMAIL_OPENEDBY = {-1, -2, -3}

_BR_NEWLINE = re.compile(r"<br\s*/?>[ \t]*\r?\n?", re.IGNORECASE)


# =========================================================================== text


def hesk_text(value: object) -> str:
    """HESK stores user text HTML-escaped, with <br /> added by nl2br() and URLs wrapped
    in <a> tags. Tags must be stripped *before* un-escaping, or text the customer typed
    (e.g. "5 < 10") would be mistaken for markup."""
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n")
    if "<" not in text and "&" not in text:
        return text.strip()
    return html_to_text(_BR_NEWLINE.sub("<br>", text))


def hesk_line(value: object) -> str:
    return re.sub(r"\s+", " ", hesk_text(value)).strip()


def attachment_ids(value: object) -> list[int]:
    """HESK attachment lists look like "12#report.pdf,13#screen.png,"."""
    ids = []
    for item in str(value or "").split(","):
        head = item.split("#", 1)[0].strip()
        if head.isdigit():
            ids.append(int(head))
    return ids


def lang_name(value: object) -> str:
    """Custom field/status names are JSON objects keyed by language."""
    try:
        names = json.loads(value or "")
    except (TypeError, ValueError):
        return hesk_line(value)
    if isinstance(names, dict) and names:
        return hesk_line(names.get("English") or next(iter(names.values())))
    return hesk_line(value)


def as_utc(value: datetime | None) -> datetime | None:
    # The connection runs with time_zone='+00:00', so TIMESTAMP values arrive in UTC.
    return value.replace(tzinfo=UTC) if isinstance(value, datetime) else None


# =========================================================================== source


class HeskSource:
    """Read-only access to a HESK database."""

    def __init__(self, url: str, prefix: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_]*", prefix):
            raise SystemExit("--prefix may only contain letters, digits and underscores")
        parts = urlsplit(url)
        if parts.scheme not in ("mysql", "mariadb") or not parts.hostname or not parts.path.strip("/"):
            raise SystemExit("--hesk-db must look like mysql://user:password@host:3306/database")
        self.prefix = prefix
        self.conn = pymysql.connect(
            host=parts.hostname,
            port=parts.port or 3306,
            user=unquote(parts.username or ""),
            password=unquote(parts.password or ""),
            database=parts.path.strip("/"),
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            init_command="SET time_zone = '+00:00'",
            read_timeout=120,
        )
        self.tables = {row["t"] for row in self.q("SELECT table_name AS t FROM information_schema.tables WHERE table_schema = DATABASE()")}
        if self.t("tickets") not in self.tables:
            raise SystemExit(f"No {self.t('tickets')} table found. Is --prefix right? (tables: {sorted(self.tables)[:10]})")
        # HESK 3.5+ customer accounts
        self.has_customers = {self.t("customers"), self.t("ticket_to_customer")} <= self.tables

    def t(self, name: str) -> str:
        return f"{self.prefix}{name}"

    def q(self, sql: str, args: Iterable | None = None) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            return list(cur.fetchall())

    def optional(self, table: str, sql: str) -> list[dict]:
        return self.q(sql.format(t=f"`{self.t(table)}`")) if self.t(table) in self.tables else []

    def users(self) -> list[dict]:
        return self.optional("users", "SELECT id, user, name, email, isadmin FROM {t}")

    def categories(self) -> dict[int, str]:
        return {r["id"]: hesk_line(r["name"]) for r in self.optional("categories", "SELECT id, name FROM {t}")}

    def custom_fields(self) -> dict[int, str]:
        rows = self.optional("custom_fields", "SELECT id, `use`, name FROM {t}")
        return {r["id"]: lang_name(r["name"]) or f"Custom field {r['id']}" for r in rows if str(r.get("use")) != "0"}

    def custom_statuses(self) -> dict[int, str]:
        return {r["id"]: lang_name(r["name"]) for r in self.optional("custom_statuses", "SELECT id, name FROM {t}")}

    def ticket_ids(self, limit: int | None) -> list[int]:
        sql = f"SELECT id FROM `{self.t('tickets')}` ORDER BY id"
        return [r["id"] for r in self.q(sql + (f" LIMIT {int(limit)}" if limit else ""))]

    def batch(self, ids: list[int]) -> Batch:
        marks = ",".join(["%s"] * len(ids))
        tickets = self.q(f"SELECT * FROM `{self.t('tickets')}` WHERE id IN ({marks}) ORDER BY id", ids)
        replies: dict[int, list[dict]] = defaultdict(list)
        for r in self.q(f"SELECT * FROM `{self.t('replies')}` WHERE replyto IN ({marks}) ORDER BY dt, id", ids):
            replies[r["replyto"]].append(r)
        notes: dict[int, list[dict]] = defaultdict(list)
        if self.t("notes") in self.tables:
            for n in self.q(f"SELECT * FROM `{self.t('notes')}` WHERE ticket IN ({marks}) ORDER BY dt, id", ids):
                notes[n["ticket"]].append(n)
        attachments: dict[int, dict] = {}
        tracks = [t["trackid"] for t in tickets]
        if tracks and self.t("attachments") in self.tables:
            tmarks = ",".join(["%s"] * len(tracks))
            for a in self.q(f"SELECT * FROM `{self.t('attachments')}` WHERE ticket_id IN ({tmarks})", tracks):
                attachments[a["att_id"]] = a
        links: dict[int, list[dict]] = defaultdict(list)
        customers: dict[int, dict] = {}
        if self.has_customers:
            for row in self.q(
                f"SELECT tc.ticket_id, tc.customer_type, c.id, c.name, c.email "
                f"FROM `{self.t('ticket_to_customer')}` tc JOIN `{self.t('customers')}` c ON c.id = tc.customer_id "
                f"WHERE tc.ticket_id IN ({marks}) ORDER BY tc.id", ids,
            ):
                links[row["ticket_id"]].append(row)
                customers[row["id"]] = row
            # Customers who replied without being linked to the ticket (rare, but possible).
            extra = {r.get("customer_id") for rs in replies.values() for r in rs} - set(customers) - {None, 0}
            if extra:
                cmarks = ",".join(["%s"] * len(extra))
                for row in self.q(f"SELECT id, name, email FROM `{self.t('customers')}` WHERE id IN ({cmarks})", list(extra)):
                    customers[row["id"]] = row
        return Batch(tickets, replies, notes, attachments, links, customers)


@dataclass
class Batch:
    tickets: list[dict]
    replies: dict[int, list[dict]]
    notes: dict[int, list[dict]]
    attachments: dict[int, dict]
    links: dict[int, list[dict]]    # ticket id -> ticket_to_customer rows (3.5+)
    customers: dict[int, dict]      # customer id -> customer (3.5+)


@dataclass
class People:
    name: str | None
    email: str | None
    others: list[str]               # followers / extra addresses


def split_emails(value: object) -> list[str]:
    return [e.strip().lower() for e in str(value or "").replace(";", ",").split(",") if e.strip()]


def ticket_people(t: dict, links: list[dict]) -> People:
    """Who the ticket belongs to, whichever HESK data layout it uses."""
    requester = next((l for l in links if l["customer_type"] == "REQUESTER"), None)
    followers = [e for l in links if l["customer_type"] != "REQUESTER" for e in split_emails(l["email"])]
    if requester is not None:
        emails = split_emails(requester["email"])
        return People(hesk_line(requester["name"]) or None, emails[0] if emails else None, emails[1:] + followers)
    # HESK <= 3.4 (name/email) or an upgraded install's preserved columns (u_name/u_email).
    name = t.get("name") if "name" in t else t.get("u_name")
    emails = split_emails(t.get("email") if "email" in t else t.get("u_email"))
    return People(hesk_line(name) or None, emails[0] if emails else None, emails[1:] + followers)


# =========================================================================== import


@dataclass
class Report:
    tickets_created: int = 0
    tickets_updated: int = 0
    tickets_unchanged: int = 0
    tickets_kept_local: int = 0
    comments_added: int = 0
    attachments_copied: int = 0
    attachments_missing: list[str] = field(default_factory=list)
    attachments_skipped: int = 0
    staff_matched: int = 0
    staff_created: int = 0
    tickets_without_email: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Options:
    attachments_dir: Path | None
    dry_run: bool
    status_map: dict[int, TicketStatus]


class HeskImporter:
    def __init__(self, source: HeskSource, db: Session, opts: Options) -> None:
        self.src = source
        self.db = db
        self.opts = opts
        self.report = Report()
        self.attachment_root = get_settings().attachment_dir
        self.categories = source.categories()
        self.custom_fields = source.custom_fields()
        self.custom_statuses = source.custom_statuses()
        self.staff: dict[int, int] = {}          # HESK user id -> our user id
        self.staff_names: dict[int, str] = {}

    # ------------------------------------------------------------------ staff

    def map_staff(self, referenced: dict[int, str]) -> None:
        """Match HESK staff to our users by email; create inactive agents for the rest
        (including staff since deleted from HESK) so history keeps its authors."""
        existing = {u.email: u for u in self.db.scalars(select(User))}
        hesk_users = {u["id"]: u for u in self.src.users()}
        for hid, fallback_name in referenced.items():
            row = hesk_users.get(hid)
            name = hesk_line(row["name"] if row else "") or (row and hesk_line(row["user"])) or fallback_name or f"HESK staff #{hid}"
            email = (row["email"] if row else "").strip().lower() or f"hesk-staff-{hid}@{NO_EMAIL_DOMAIN}"
            user = existing.get(email)
            if user is None:
                user = User(
                    email=email,
                    full_name=name[:200],
                    role=UserRole.AGENT,
                    is_active=False,  # an admin activates + sets a password if they still work here
                    password_hash=hash_password(secrets.token_urlsafe(32)),
                )
                self.db.add(user)
                self.db.flush()
                existing[email] = user
                self.report.staff_created += 1
                log.info("Staff %s <%s>: created as inactive agent", name, email)
            else:
                self.report.staff_matched += 1
                log.info("Staff %s <%s>: matched existing user", name, email)
            self.staff[hid] = user.id
            self.staff_names[hid] = name

    def collect_staff(self, ids: list[int]) -> dict[int, str]:
        """Every HESK staff id that appears as owner, opener, replier or note author."""
        found: dict[int, str] = {}
        for chunk in _chunks(ids):
            batch = self.src.batch(chunk)
            tickets, replies, notes = batch.tickets, batch.replies, batch.notes
            for t in tickets:
                for key in ("owner", "openedby"):
                    if (t.get(key) or 0) > 0:
                        found.setdefault(int(t[key]), "")
            for rs in replies.values():
                for r in rs:
                    if (r.get("staffid") or 0) > 0:
                        found[int(r["staffid"])] = found.get(int(r["staffid"])) or hesk_line(r.get("name"))
            for ns in notes.values():
                for n in ns:
                    if (n.get("who") or 0) > 0:
                        found.setdefault(int(n["who"]), "")
        return found

    # ------------------------------------------------------------------ tickets

    def run(self, limit: int | None) -> Report:
        ids = self.src.ticket_ids(limit)
        log.info("HESK tickets to process: %d", len(ids))
        self.map_staff(self.collect_staff(ids))
        self._commit()
        for chunk in _chunks(ids):
            batch = self.src.batch(chunk)
            for t in batch.tickets:
                savepoint = self.db.begin_nested()
                try:
                    self.import_ticket(t, batch)
                    savepoint.commit()
                except Exception as exc:  # one bad ticket must not stop the import
                    savepoint.rollback()
                    log.exception("Ticket %s failed", t.get("trackid"))
                    self.report.failed.append((t.get("trackid", f"id {t.get('id')}"), repr(exc)))
                self._commit()
            self.db.expunge_all()  # keep memory flat on large help desks
        if self.opts.dry_run:
            self.db.rollback()
        return self.report

    def _commit(self) -> None:
        if not self.opts.dry_run:
            self.db.commit()

    def import_ticket(self, t: dict, batch: Batch) -> None:
        trackid = t["trackid"]
        replies, notes, attachments = batch.replies.get(t["id"], []), batch.notes.get(t["id"], []), batch.attachments
        people = ticket_people(t, batch.links.get(t["id"], []))
        fields = self.ticket_fields(t, people)
        ticket = self.db.scalar(select(Ticket).where(Ticket.legacy_ref == trackid))
        created = ticket is None
        if created:
            ticket = Ticket(legacy_ref=trackid, message_id=None, **fields)
            self.db.add(ticket)
            self.db.flush()
            self.report.tickets_created += 1
            if fields["requester_email"].endswith("@" + NO_EMAIL_DOMAIN):
                self.report.tickets_without_email += 1
        known = set(self.db.scalars(select(TicketComment.legacy_ref).where(TicketComment.ticket_id == ticket.id)))
        has_local = bool(self.db.scalar(
            select(func.count()).select_from(TicketComment)
            .where(TicketComment.ticket_id == ticket.id, TicketComment.legacy_ref.is_(None))
        ))

        referenced: set[int] = set()
        added = 0

        # Summary note: everything HESK knew that has no column here.
        summary_ref = f"hesk:ticket:{trackid}"
        summary_body = self.summary(t, people)
        if summary_ref not in known:
            self.db.add(TicketComment(
                ticket_id=ticket.id, author_name=IMPORT_AUTHOR, body=summary_body, is_internal=True,
                source=MessageSource.IMPORT, legacy_ref=summary_ref, created_at=fields["created_at"],
            ))
            added += 1
        elif not has_local:
            note = self.db.scalar(select(TicketComment).where(TicketComment.legacy_ref == summary_ref))
            note.body = summary_body

        # Attachments of the opening message.
        ticket_att = attachment_ids(t.get("attachments"))
        referenced.update(ticket_att)
        if created:
            self.copy_attachments(ticket, None, ticket_att, attachments)

        for r in replies:
            ref = f"hesk:reply:{r['id']}"
            ids = attachment_ids(r.get("attachments"))
            referenced.update(ids)
            if ref in known:
                continue
            staff_id = int(r.get("staffid") or 0)
            if staff_id:
                author_email, author_name = None, hesk_line(r.get("name")) or self.staff_names.get(staff_id)
            else:
                # 3.5+: replies reference a customer; older: a name column; else the requester.
                customer = batch.customers.get(r.get("customer_id") or 0)
                emails = split_emails(customer["email"]) if customer else []
                author_email = emails[0] if emails else fields["requester_email"]
                author_name = (hesk_line(customer["name"]) if customer else "") or hesk_line(r.get("name")) or fields["requester_name"]
            comment = TicketComment(
                ticket_id=ticket.id,
                author_id=self.staff.get(staff_id) if staff_id else None,
                author_email=author_email,
                author_name=author_name,
                body=hesk_text(r.get("message")),
                body_html=sanitize_html(r["message_html"]) if r.get("message_html") else None,
                is_internal=False,
                source=MessageSource.IMPORT,
                legacy_ref=ref,
                created_at=as_utc(r.get("dt")) or fields["created_at"],
            )
            self.db.add(comment)
            self.db.flush()
            self.copy_attachments(ticket, comment, ids, attachments)
            added += 1

        for n in notes:
            ref = f"hesk:note:{n['id']}"
            ids = attachment_ids(n.get("attachments"))
            referenced.update(ids)
            if ref in known:
                continue
            who = int(n.get("who") or 0)
            comment = TicketComment(
                ticket_id=ticket.id,
                author_id=self.staff.get(who),
                author_name=self.staff_names.get(who) or "HESK staff",
                body=hesk_text(n.get("message")),
                is_internal=True,
                source=MessageSource.IMPORT,
                legacy_ref=ref,
                created_at=as_utc(n.get("dt")) or fields["created_at"],
            )
            self.db.add(comment)
            self.db.flush()
            self.copy_attachments(ticket, comment, ids, attachments)
            added += 1

        # Files HESK has for this ticket that no message lists: keep them on the ticket.
        if created:
            orphans = [aid for aid, a in attachments.items() if a["ticket_id"] == trackid and aid not in referenced]
            self.copy_attachments(ticket, None, orphans, attachments)

        self.report.comments_added += added
        if created:
            return
        if has_local:
            # Agents already worked this ticket here; don't overwrite their changes.
            self.report.tickets_kept_local += 1
            return
        changed = False
        for key in ("subject", "status", "priority", "assigned_to_id", "resolved_at", "last_activity_at", "requester_name"):
            if getattr(ticket, key) != fields[key]:
                setattr(ticket, key, fields[key])
                changed = True
        if changed or added:
            self.report.tickets_updated += 1
        else:
            self.report.tickets_unchanged += 1

    def ticket_fields(self, t: dict, people: People) -> dict:
        trackid = t["trackid"]
        status_code = int(t.get("status") or 0)
        status = self.opts.status_map.get(status_code) or STATUS_MAP.get(status_code, CUSTOM_STATUS_DEFAULT)
        created_at = as_utc(t.get("dt")) or datetime.now(UTC)
        last_change = as_utc(t.get("lastchange")) or created_at
        resolved_at = (as_utc(t.get("closedat")) or last_change) if status in CLOSED_STATUSES else None
        owner = int(t.get("owner") or 0)
        opened_by = int(t.get("openedby") or 0)
        return {
            "subject": (hesk_line(t.get("subject")) or "(no subject)")[:998],
            "description": hesk_text(t.get("message")),
            "description_html": sanitize_html(t["message_html"]) if t.get("message_html") else None,
            "status": status,
            "priority": PRIORITY_MAP.get(str(t.get("priority")), TicketPriority.MEDIUM),
            "source": MessageSource.EMAIL if opened_by in EMAIL_OPENEDBY else MessageSource.WEB,
            "requester_email": people.email or f"no-email+{trackid.lower()}@{NO_EMAIL_DOMAIN}",
            "requester_name": people.name,
            "assigned_to_id": self.staff.get(owner) if owner else None,
            "created_by_id": self.staff.get(opened_by) if opened_by > 0 else None,
            "created_at": created_at,
            "updated_at": last_change,
            "last_activity_at": last_change,
            "resolved_at": resolved_at,
        }

    def summary(self, t: dict, people: People) -> str:
        code = int(t.get("status") or 0)
        status = HESK_STATUS_NAMES.get(code) or self.custom_statuses.get(code) or f"custom #{code}"
        opened_by = int(t.get("openedby") or 0)
        via = OPENED_VIA.get(opened_by) or f"staff ({self.staff_names.get(opened_by, f'#{opened_by}')})"
        lines = [
            f"Imported from HESK ticket {t['trackid']}.",
            f"HESK status: {status} · priority: {PRIORITY_NAMES.get(str(t.get('priority')), '?')}"
            f" · category: {self.categories.get(t.get('category'), t.get('category'))}",
            f"Opened via: {via}",
        ]
        if people.others:
            lines.append("Other addresses (CC): " + ", ".join(dict.fromkeys(people.others)))
        for fid, name in sorted(self.custom_fields.items()):
            value = hesk_line(t.get(f"custom{fid}"))
            if value:
                lines.append(f"{name}: {value}")
        if str(t.get("time_worked") or "0:00:00") not in ("0:00:00", "00:00:00"):
            lines.append(f"Time worked: {t['time_worked']}")
        if t.get("due_date"):
            lines.append(f"Due date: {as_utc(t['due_date']):%Y-%m-%d %H:%M} UTC")
        history = [hesk_line(item) for item in str(t.get("history") or "").split("</li>")]
        history = [h for h in history if h]
        if history:
            lines += ["", "HESK history:"] + [f"  {h}" for h in history]
        return "\n".join(lines)

    # ------------------------------------------------------------------ files

    def copy_attachments(self, ticket: Ticket, comment: TicketComment | None, ids: list[int], attachments: dict[int, dict]) -> None:
        for aid in ids:
            att = attachments.get(aid)
            if att is None:
                continue  # listed in the message but deleted from HESK
            if self.opts.attachments_dir is None:
                self.report.attachments_skipped += 1
                continue
            # saved_name comes from the database: never let it escape the folder.
            path = self.opts.attachments_dir / Path(str(att["saved_name"])).name
            if not path.is_file():
                self.report.attachments_missing.append(f"{ticket.legacy_ref}: {att['real_name']} ({path.name})")
                continue
            data = path.read_bytes()
            if self.opts.dry_run:
                rel_path, digest = "(dry run)", hashlib.sha256(data).hexdigest()
            else:
                rel_path, digest = write_blob(self.attachment_root, data)
            filename = hesk_line(att["real_name"])[:255] or path.name
            self.db.add(TicketAttachment(
                ticket_id=ticket.id,
                comment_id=comment.id if comment else None,
                filename=filename,
                content_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
                size_bytes=len(data),
                sha256=digest,
                storage_path=rel_path,
            ))
            self.report.attachments_copied += 1


def _chunks(items: list[int], size: int = CHUNK) -> Iterator[list[int]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


# =========================================================================== CLI


def parse_status_map(value: str) -> dict[int, TicketStatus]:
    mapping = {}
    for part in filter(None, (p.strip() for p in value.split(","))):
        code, _, target = part.partition("=")
        try:
            mapping[int(code)] = TicketStatus(target.strip().lower())
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"bad --status-map entry {part!r}: use CODE=STATUS with STATUS one of "
                + ", ".join(s.value for s in TicketStatus)
            )
    return mapping


def print_report(r: Report, dry_run: bool) -> None:
    title = "DRY RUN: nothing was saved" if dry_run else "Import finished"
    print(f"\n=== {title} ===")
    print(f"Tickets created:         {r.tickets_created}")
    print(f"Tickets updated:         {r.tickets_updated}   (already imported; new HESK activity applied)")
    print(f"Tickets unchanged:       {r.tickets_unchanged}")
    print(f"Tickets kept as-is:      {r.tickets_kept_local}   (already worked on here; only new replies/notes added)")
    print(f"Replies/notes added:     {r.comments_added}")
    print(f"Attachments copied:      {r.attachments_copied}")
    if r.attachments_skipped:
        print(f"Attachments skipped:     {r.attachments_skipped}   (no --attachments-dir given)")
    print(f"Staff matched / created: {r.staff_matched} / {r.staff_created}   (created accounts are inactive)")
    if r.tickets_without_email:
        print(f"Tickets without email:   {r.tickets_without_email}   (placeholder @{NO_EMAIL_DOMAIN} address)")
    if r.attachments_missing:
        print(f"\nMissing attachment files ({len(r.attachments_missing)}):")
        for m in r.attachments_missing[:50]:
            print(f"  {m}")
    if r.failed:
        print(f"\nFAILED tickets ({len(r.failed)}):")
        for trackid, err in r.failed[:50]:
            print(f"  {trackid}: {err}")


def purge(yes: bool) -> int:
    with SessionLocal() as db:
        count = db.scalar(select(func.count()).select_from(Ticket).where(Ticket.legacy_ref.is_not(None)))
        if not yes:
            print(f"This permanently deletes {count} imported ticket(s), including any replies agents "
                  "added to them here. Re-run with --purge --yes to confirm.")
            return 1
        db.execute(delete(Ticket).where(Ticket.legacy_ref.is_not(None)))
        db.commit()
    print(f"Deleted {count} imported ticket(s). Staff accounts created by the import were kept.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.hesk_import", description=__doc__.split("\n\n")[0])
    parser.add_argument("--hesk-db", default=os.environ.get("HESK_DB_URL"),
                        help="mysql://user:password@host:3306/database (or set HESK_DB_URL)")
    parser.add_argument("--prefix", default="hesk_", help="HESK table prefix (default: hesk_)")
    parser.add_argument("--attachments-dir", type=Path, help="HESK's attachments folder, mounted into the container")
    parser.add_argument("--status-map", type=parse_status_map, default={},
                        help="override status mapping for HESK status codes, e.g. 6=resolved,7=pending")
    parser.add_argument("--limit", type=int, help="only import the first N HESK tickets (for a trial run)")
    parser.add_argument("--dry-run", action="store_true", help="do everything, report, then roll back")
    parser.add_argument("--purge", action="store_true", help="delete all previously imported tickets")
    parser.add_argument("--yes", action="store_true", help="confirm --purge")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")

    if args.purge:
        return purge(args.yes)
    if not args.hesk_db:
        parser.error("--hesk-db (or HESK_DB_URL) is required")
    if args.attachments_dir and not args.attachments_dir.is_dir():
        parser.error(f"--attachments-dir {args.attachments_dir} is not a directory (is it mounted?)")

    source = HeskSource(args.hesk_db, args.prefix)
    opts = Options(attachments_dir=args.attachments_dir, dry_run=args.dry_run, status_map=args.status_map)
    with SessionLocal() as db:
        report = HeskImporter(source, db, opts).run(args.limit)
    print_report(report, args.dry_run)
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
