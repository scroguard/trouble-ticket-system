"""Customer-reply alerts to staff (email + portal replies). Fresh stack, no seed."""
from common import BASE, COMPOSE, IMAP_PORT, REPO, SHOTS, SMTP_PORT, psql  # noqa: F401
import imaplib, re, smtplib, subprocess, sys, time
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import requests

B = BASE
PROJ = REPO
CUST = "hank@client.example.com"
STAFF = ["ada@example.com", "alice@example.com", "bob@example.com"]
ok = True


def check(cond, label):
    global ok
    print(("PASS " if cond else "FAIL ") + label, flush=True)
    ok &= bool(cond)


def psql(q):
    return subprocess.run([*COMPOSE, "exec", "-T", "db", "psql", "-U", "tickets", "-d", "tickets", "-tA", "-c", q],
                          cwd=PROJ, capture_output=True, text=True).stdout.strip()


def subjects(user):
    c = imaplib.IMAP4("localhost", IMAP_PORT); c.login(user, "x"); c.select("INBOX")
    out = [BytesParser(policy=policy.default).parsebytes(c.fetch(i, "(BODY.PEEK[])")[1][0][1]) for i in c.search(None, "ALL")[1][0].split()]
    c.logout()
    return out


def alerts(user, tag):
    return [m for m in subjects(user) if "Customer replied" in m["Subject"] and tag in m["Subject"]]


def mail(subject, body, frm=CUST, headers=None):
    m = EmailMessage(); m["From"] = frm; m["To"] = "support@example.com"; m["Subject"] = subject
    for k, v in (headers or {}).items():
        m[k] = v
    m.set_content(body)
    with smtplib.SMTP("localhost", SMTP_PORT) as c:
        c.send_message(m)


def wait(fn, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        if r := fn():
            return r
        time.sleep(1)
    return None


def counts(tag):
    return {u: len(alerts(u, tag)) for u in STAFF}


def expect_counts(tag, expected, label, settle=12):
    got = wait(lambda: counts(tag) if counts(tag) == expected else None, 40)
    time.sleep(settle)  # and nothing extra arrives afterwards
    final = counts(tag)
    check(got is not None and final == expected, f"{label}: {final}")


requests.post(f"{B}/auth/register", json={"email": "ada@example.com", "full_name": "Ada Admin", "password": "correct-horse-1"}).raise_for_status()
ada = requests.Session(); ada.post(f"{B}/auth/login", json={"email": "ada@example.com", "password": "correct-horse-1"}).raise_for_status()
for e, n in [("alice@example.com", "Alice Agent"), ("bob@example.com", "Bob Agent")]:
    ada.post(f"{B}/auth/register", json={"email": e, "full_name": n, "password": "agent-pass-123"}).raise_for_status()
ids = {u["email"]: u["id"] for u in ada.get(f"{B}/api/users").json()}
alice = requests.Session(); alice.post(f"{B}/auth/login", json={"email": "alice@example.com", "password": "agent-pass-123"})
H = {"Origin": B}

# --- new ticket: alert reaches agents AND admins now
mail("Label printer offline", "It stopped printing.")
t = wait(lambda: (ada.get(f"{B}/api/tickets", params={"q": "label printer"}).json()["items"] or [None])[0])
tid, tag = t["id"], f"[TICKET-{t['id']}]"
new = wait(lambda: all(any("New Ticket Created" in m["Subject"] and tag in m["Subject"] for m in subjects(u)) for u in STAFF))
check(new, "new-ticket alert goes to admins as well as agents")

# --- unassigned: a customer reply alerts all staff
mail(f"Re: {tag} Label printer offline", "Any update? We have orders waiting.")
expect_counts(tag, {u: 1 for u in STAFF}, "unassigned ticket: customer reply alerts all staff")
m = alerts("ada@example.com", tag)[0]
body = m.get_body(("plain",)).get_content()
check(m["Subject"].startswith("[MEDIUM] Customer replied:") and "Any update? We have orders waiting." in body
      and "via email" in body and f"{B}/#/tickets/{tid}" in body, "alert: priority, excerpt, channel, dashboard link")
check(m["Auto-Submitted"] == "auto-generated", "alert is marked automated (no auto-reply loops)")

# --- burst within the cooldown: no second alert
mail(f"Re: {tag} Label printer offline", "Also the backup printer.")
expect_counts(tag, {u: 1 for u in STAFF}, "second reply within cooldown: no extra alert")

# --- an agent answers; the next customer reply alerts again despite the cooldown
alice.post(f"{B}/api/tickets/{tid}/comments", json={"body": "Checking now."}, headers=H)
mail(f"Re: {tag} Label printer offline", "Thanks, still offline though.")
expect_counts(tag, {u: 2 for u in STAFF}, "agent answered in between: alert again")

# --- assigned: only the assignee
ada.patch(f"{B}/api/tickets/{tid}", json={"assigned_to": ids["bob@example.com"]}, headers=H)
psql(f"update tickets set customer_reply_alerted_at = now() - interval '10 minutes' where id={tid}")
mail(f"Re: {tag} Label printer offline", "Hello?")
expect_counts(tag, {"ada@example.com": 2, "alice@example.com": 2, "bob@example.com": 3}, "assigned ticket: only the assignee is alerted")

# --- resolved ticket reopened by a reply
ada.patch(f"{B}/api/tickets/{tid}", json={"status": "resolved"}, headers=H)
psql(f"update tickets set customer_reply_alerted_at = now() - interval '10 minutes' where id={tid}")
mail(f"Re: {tag} Label printer offline", "It broke again.")
expect_counts(tag, {"ada@example.com": 2, "alice@example.com": 2, "bob@example.com": 4}, "reply to a resolved ticket alerts the assignee")
check(any("Customer replied (ticket reopened)" in m["Subject"] for m in alerts("bob@example.com", tag)), "alert says the ticket was reopened")

# --- staff emailing into the thread (internal note) doesn't alert
psql(f"update tickets set customer_reply_alerted_at = now() - interval '10 minutes' where id={tid}")
mail(f"Re: {tag} Label printer offline", "note from bob by email", frm="bob@example.com")
expect_counts(tag, {"ada@example.com": 2, "alice@example.com": 2, "bob@example.com": 4}, "staff email into the thread: no alert")

# --- portal reply alerts too
portal = requests.Session()
portal.post(f"{B}/portal/api/request-link", json={"email": CUST})
link = wait(lambda: next((re.search(r"#/verify/([\w-]+)", m.get_body(("plain",)).get_content()).group(1)
                          for m in subjects(CUST) if m["Subject"].startswith("Your sign-in link")), None))
portal.post(f"{B}/portal/api/verify", json={"token": link}).raise_for_status()
portal.post(f"{B}/portal/api/tickets/{tid}/messages", data={"message": "Replying from the portal."}).raise_for_status()
expect_counts(tag, {"ada@example.com": 2, "alice@example.com": 2, "bob@example.com": 5}, "portal reply alerts the assignee")
last = alerts("bob@example.com", tag)
check(any("via the customer portal" in m.get_body(("plain",)).get_content() for m in last), "alert says it came via the portal")

# --- deactivated assignee: falls back to everyone active
ada.patch(f"{B}/api/users/{ids['bob@example.com']}", json={"is_active": False}, headers=H)
psql(f"update tickets set customer_reply_alerted_at = now() - interval '10 minutes' where id={tid}")
mail(f"Re: {tag} Label printer offline", "Is anyone there?")
expect_counts(tag, {"ada@example.com": 3, "alice@example.com": 3, "bob@example.com": 5}, "deactivated assignee: all active staff alerted instead")

# --- auto-replies never alert
psql(f"update tickets set customer_reply_alerted_at = now() - interval '10 minutes' where id={tid}")
mail(f"Re: {tag} Label printer offline", "I am out of office", headers={"Auto-Submitted": "auto-replied"})
expect_counts(tag, {"ada@example.com": 3, "alice@example.com": 3, "bob@example.com": 5}, "out-of-office replies don't alert")

sys.exit(0 if ok else 1)
