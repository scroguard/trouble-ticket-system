import imaplib, smtplib, time, sys
from email.message import EmailMessage
from email import policy
from email.parser import BytesParser
from email.utils import make_msgid
from sqlalchemy import select, func

from app.config import get_settings
from app.db import session_scope
from app.email_service import EmailService, is_transient, EmailError
from app.models import Ticket, TicketComment, TicketAttachment, User, TicketStatus, MessageSource, TicketPriority

S = get_settings()
svc = EmailService(S)
ok = True


def check(cond, label):
    global ok
    print(("PASS " if cond else "FAIL ") + label)
    ok &= bool(cond)


def send(frm, subject, text=None, html=None, headers=None, attach=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, "support@example.com", subject
    m["Message-ID"] = make_msgid(domain="client.test")
    for k, v in (headers or {}).items():
        m[k] = v
    if text:
        m.set_content(text)
    if html:
        if text:
            m.add_alternative(html, subtype="html")
        else:
            m.set_content(html, subtype="html")
    if attach:
        m.add_attachment(attach[1], maintype="application", subtype="octet-stream", filename=attach[0])
    with smtplib.SMTP("mail", 3025) as s:
        s.send_message(m)
    return m["Message-ID"]


def inbox(user):
    c = imaplib.IMAP4("mail", 3143)
    c.login(user, "x")
    c.select("INBOX")
    _, d = c.search(None, "ALL")
    msgs = []
    for n in d[0].split():
        _, data = c.fetch(n, "(BODY.PEEK[])")
        msgs.append(BytesParser(policy=policy.default).parsebytes(data[0][1]))
    c.logout()
    return msgs


def wait_for(fn, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if r := fn():
            return r
        time.sleep(1)
    return None


def ticket_by_msgid(mid):
    with session_scope() as db:
        return db.scalar(select(Ticket).where(Ticket.message_id == mid))


def comments(tid):
    with session_scope() as db:
        return db.scalars(select(TicketComment).where(TicketComment.ticket_id == tid).order_by(TicketComment.id)).all()


# 1. New ticket with HTML + attachment
mid1 = send(
    '"Carol Customer" <carol@client.test>', "Printer on fire",
    text="The printer is on fire.\nPlease help.",
    html="<p>The printer is <b>on fire</b>.</p><script>alert(1)</script><img src='http://track/x.gif'>",
    attach=("../../etc/passwd.log", b"log-bytes" * 10),
)
t = wait_for(lambda: ticket_by_msgid(mid1))
check(t is not None, "new email creates ticket")
tid = t.id
check(t.requester_email == "carol@client.test" and t.requester_name == "Carol Customer", "requester parsed")
check("<script>" not in (t.description_html or "") and "<img" not in t.description_html and "<b>on fire</b>" in t.description_html, "HTML sanitized")
with session_scope() as db:
    atts = db.scalars(select(TicketAttachment).where(TicketAttachment.ticket_id == tid)).all()
check(len(atts) == 1 and atts[0].filename == "passwd.log" and (S.attachment_dir / atts[0].storage_path).exists(), "attachment stored with safe filename")

check(t.priority == TicketPriority.MEDIUM, "plain ticket defaults to MEDIUM")

# 2. Broadcast to agents + ack to customer
tag = f"[TICKET-{tid}]"
alice = wait_for(lambda: [m for m in inbox("alice@example.com") if tag in m["Subject"] and "New Ticket Created" in m["Subject"]])
bob = wait_for(lambda: [m for m in inbox("bob@example.com") if tag in m["Subject"]])
admin = wait_for(lambda: [m for m in inbox("admin@example.com") if tag in m["Subject"]])
check(alice and bob, "broadcast reached both agents")
check(any("New Ticket Created" in m["Subject"] for m in admin or []), "admins receive new-ticket alerts too")
check(alice and alice[0]["Auto-Submitted"] == "auto-generated", "alert marked Auto-Submitted")
check(alice and alice[0]["Subject"] == f"[MEDIUM] New Ticket Created: {tag} Printer on fire", "alert subject leads with priority")
check(alice and "PRIORITY: MEDIUM" in alice[0].get_body(("plain",)).get_content() and alice[0]["X-Priority"] is None, "medium alert: banner, no X-Priority")
ack = wait_for(lambda: [m for m in inbox("carol@client.test") if tag in m["Subject"]])
check(ack and ack[0]["In-Reply-To"] == mid1, "customer ack threaded to original")

# 3. Customer replies to the ack (References chain) -> comment, quoted text stripped
mid2 = send("carol@client.test", f"Re: {tag} Printer on fire",
            text="Now it's smoking too.\n\nOn Mon, Sep 29, 2026 Support wrote:\n> We received your request",
            headers={"In-Reply-To": ack[0]["Message-ID"], "References": f"{mid1} {ack[0]['Message-ID']}"})
c = wait_for(lambda: [x for x in comments(tid) if x.message_id == mid2])
check(c and c[0].body == "Now it's smoking too." and not c[0].is_internal, "header-threaded reply appended, quote stripped")

# 4. Subject-tag only from requester -> appended; from stranger -> new ticket
mid3 = send("carol@client.test", f"{tag} also the fax", text="fax too")
check(wait_for(lambda: [x for x in comments(tid) if x.message_id == mid3]), "subject-tag reply from requester appended")
mid4 = send("mallory@evil.test", f"Re: {tag} lol", text="injected")
t4 = wait_for(lambda: ticket_by_msgid(mid4))
check(t4 is not None and t4.id != tid, "subject-tag from stranger opens separate ticket")

# 5. Auto-reply ignored
mid5 = send("carol@client.test", f"Out of office {tag}", text="away", headers={"Auto-Submitted": "auto-replied"})
time.sleep(S.imap_poll_interval + 4)
with session_scope() as db:
    n = db.scalar(select(func.count()).select_from(TicketComment).where(TicketComment.message_id == mid5))
    n += db.scalar(select(func.count()).select_from(Ticket).where(Ticket.message_id == mid5))
check(n == 0, "auto-reply ignored (loop prevention)")

# 6. Agent reply from dashboard -> customer, threaded
with session_scope() as db:
    ticket = db.get(Ticket, tid)
    agent = db.scalar(select(User).where(User.email == "alice@example.com"))
    reply = TicketComment(ticket=ticket, author_id=agent.id, body="Please step away from the printer.", source=MessageSource.WEB)
    db.add(reply)
    db.flush()
    sent = svc.send_reply_to_customer(ticket, reply, agent)
    reply_mid = reply.message_id
    check(sent and reply.delivery_status.value == "sent", "agent reply delivered")
got = wait_for(lambda: [m for m in inbox("carol@client.test") if m["Message-ID"] == reply_mid])
check(got and got[0]["Subject"] == f"Re: {tag} Printer on fire" and got[0]["In-Reply-To"] == mid3
      and mid1 in got[0]["References"], "customer got threaded reply (subject tag, In-Reply-To, References)")

# 7. Customer replies to agent reply with In-Reply-To only, subject tag removed
mid6 = send("carol@client.test", "Re: Printer", text="Done, thanks!", headers={"In-Reply-To": reply_mid})
check(wait_for(lambda: [x for x in comments(tid) if x.message_id == mid6]), "reply matched via outbound Message-ID")

# 8. Resolved ticket reopens on customer reply
with session_scope() as db:
    db.get(Ticket, tid).status = TicketStatus.RESOLVED
mid7 = send("carol@client.test", f"Re: {tag}", text="it's back", headers={"In-Reply-To": reply_mid})
wait_for(lambda: [x for x in comments(tid) if x.message_id == mid7])
with session_scope() as db:
    check(db.get(Ticket, tid).status == TicketStatus.OPEN, "resolved ticket reopened by customer reply")

# 9. Staff email into thread -> internal note
mid8 = send("bob@example.com", f"Re: [MEDIUM] New Ticket Created: {tag} Printer on fire", text="I'll grab an extinguisher")
c8 = wait_for(lambda: [x for x in comments(tid) if x.message_id == mid8])
check(c8 and c8[0].is_internal and c8[0].author_id is not None, "staff email reply stored as internal note")

# 10. Assignment notification
with session_scope() as db:
    ticket = db.get(Ticket, tid)
    bob_u = db.scalar(select(User).where(User.email == "bob@example.com"))
    ada = db.scalar(select(User).where(User.email == "admin@example.com"))
    check(svc.notify_assignment(ticket, bob_u, ada), "assignment notice sent")
check(wait_for(lambda: [m for m in inbox("bob@example.com") if "Assigned to you" in m["Subject"]]), "assignee received notice")

# 11. Duplicate delivery of the same Message-ID is idempotent
with session_scope() as db:
    before = db.scalar(select(func.count()).select_from(Ticket))
m = EmailMessage(); m["From"] = "carol@client.test"; m["To"] = "support@example.com"; m["Subject"] = "dup"; m["Message-ID"] = mid1; m.set_content("x")
with smtplib.SMTP("mail", 3025) as s:
    s.send_message(m)
time.sleep(S.imap_poll_interval + 4)
with session_scope() as db:
    check(db.scalar(select(func.count()).select_from(Ticket)) == before, "re-delivered Message-ID not duplicated")

# 12. Retry/backoff against a dead SMTP port
dead = S.model_copy(update={"smtp_port": 1, "email_max_retries": 2, "email_retry_base_delay": 0.1})
t0 = time.time()
try:
    EmailService(dead).send(svc.compose(to="x@y.z", subject="s", text="t"))
    check(False, "dead SMTP raises")
except EmailError as e:
    check("ConnectionRefused" in str(e), f"dead SMTP retried then failed cleanly ({time.time()-t0:.2f}s)")
check(not is_transient(smtplib.SMTPAuthenticationError(535, b"bad")), "auth errors not retried")
check(is_transient(smtplib.SMTPResponseException(451, b"later")), "4xx retried")
check(is_transient(imaplib.IMAP4.abort("bye")) and not is_transient(imaplib.IMAP4.error("LOGIN failed")), "IMAP classification")

# 13. Keyword escalation end to end
mu = send('"Dan" <dan@client.test>', "URGENT: payment server down", text="Nothing works since 9am.")
tu = wait_for(lambda: ticket_by_msgid(mu))
check(tu and tu.priority == TicketPriority.URGENT, "urgent keywords in subject -> URGENT ticket")
utag = f"[TICKET-{tu.id}]"
ua = wait_for(lambda: [m for m in inbox("alice@example.com") if utag in m["Subject"]])
check(ua and ua[0]["Subject"] == f"[URGENT] New Ticket Created: {utag} URGENT: payment server down", "URGENT alert subject")
if ua:
    body = ua[0].get_body(("plain",)).get_content(); hbody = ua[0].get_body(("html",)).get_content()
    check(body.splitlines()[1] == "PRIORITY: URGENT" and "Auto-escalated" in body, "URGENT banner + reason at top of text body")
    check("PRIORITY: URGENT" in hbody and "#dc2626" in hbody, "URGENT badge in HTML body")
    check(ua[0]["X-Priority"].startswith("1") and ua[0]["Importance"] == "high", "X-Priority/Importance headers set")
notes = [c for c in comments(tu.id) if c.source == MessageSource.SYSTEM]
check(notes and notes[0].is_internal and "Urgent" in notes[0].body, "internal audit note explains escalation")

mb = send("erin@client.test", "Login question", text="This is critical, our team can't work.")
tb = wait_for(lambda: ticket_by_msgid(mb))
check(tb and tb.priority == TicketPriority.HIGH, "strong keyword in body -> HIGH")
mn = send("fay@client.test", "Not urgent: typo on pricing page", text="no rush")
tn = wait_for(lambda: ticket_by_msgid(mn))
check(tn and tn.priority == TicketPriority.MEDIUM, "negated keyword stays MEDIUM")
mh = send("gus@client.test", "Question", text="hi", headers={"Importance": "high"})
th = wait_for(lambda: ticket_by_msgid(mh))
check(th and th.priority == TicketPriority.HIGH, "sender Importance: high -> HIGH")

# Replies never change priority (escalation is creation-only)
mr = send("carol@client.test", f"Re: {tag} URGENT EMERGENCY", text="critical outage!", headers={"In-Reply-To": reply_mid})
wait_for(lambda: [x for x in comments(tid) if x.message_id == mr])
with session_scope() as db:
    check(db.get(Ticket, tid).priority == TicketPriority.MEDIUM, "reply keywords don't re-prioritise existing ticket")

sys.exit(0 if ok else 1)
