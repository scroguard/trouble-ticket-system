"""Customer portal: black-box API test against the compose stack (+ GreenMail)."""
from common import BASE, COMPOSE, IMAP_PORT, REPO, SHOTS, SMTP_PORT, psql  # noqa: F401
import imaplib, re, smtplib, subprocess, sys, time
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import requests

B = BASE
PROJ = REPO
ok = True


def check(cond, label):
    global ok
    print(("PASS " if cond else "FAIL ") + label, flush=True)
    ok &= bool(cond)


def psql(q):
    return subprocess.run([*COMPOSE, "exec", "-T", "db", "psql", "-U", "tickets",
                           "-d", "tickets", "-tA", "-c", q], cwd=PROJ, capture_output=True, text=True).stdout.strip()


def inbox(user):
    c = imaplib.IMAP4("localhost", IMAP_PORT); c.login(user, "x"); c.select("INBOX")
    out = [BytesParser(policy=policy.default).parsebytes(c.fetch(n, "(BODY.PEEK[])")[1][0][1]) for n in c.search(None, "ALL")[1][0].split()]
    c.logout()
    return out


def body(m):
    return m.get_body(("plain",)).get_content()


def wait_for(fn, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if r := fn():
            return r
        time.sleep(0.5)
    return None


def sign_in_link(email, after=0):
    msgs = wait_for(lambda: [m for m in inbox(email) if m["Subject"].startswith("Your sign-in link")][after:] or None)
    return re.search(r"(https?://\S+/portal/#/verify/([\w-]+))", body(msgs[-1])) if msgs else None


def customer(email):
    s = requests.Session()
    n = len([m for m in inbox(email) if m["Subject"].startswith("Your sign-in link")])
    s.post(f"{B}/portal/api/request-link", json={"email": email}).raise_for_status()
    token = sign_in_link(email, n).group(2)
    s.post(f"{B}/portal/api/verify", json={"token": token}).raise_for_status()
    return s


# --- page + agent setup
r = requests.get(f"{B}/portal/")
check(r.status_code == 200 and "<title>Support Desk</title>" in r.text and "default-src 'self'" in r.headers["content-security-policy"],
      "portal page served with site name + CSP")
check(requests.get(f"{B}/portal").status_code == 200, "/portal without slash works")
requests.post(f"{B}/auth/register", json={"email": "ada@example.com", "full_name": "Ada Admin", "password": "correct-horse-1"}).raise_for_status()
agent = requests.Session()
agent.post(f"{B}/auth/login", json={"email": "ada@example.com", "password": "correct-horse-1"}).raise_for_status()
agent.post(f"{B}/auth/register", json={"email": "alice@example.com", "full_name": "Alice Agent", "password": "agent-pass-123"}).raise_for_status()

# --- sign-in links
anon = requests.Session()
check(anon.get(f"{B}/portal/api/me").status_code == 401, "portal API requires sign-in")
r = anon.post(f"{B}/portal/api/request-link", json={"email": "not-an-email"})
check(r.status_code == 400, "invalid email -> 400")
r = anon.post(f"{B}/portal/api/request-link", json={"email": "Carol@Client.example.com"})
check(r.status_code == 202 and "Check your email" in r.json()["message"], "link request accepted (202)")
m = sign_in_link("carol@client.example.com")
check(m and m.group(1).startswith(f"{B}/portal/#/verify/"), "sign-in email with link; token in URL fragment")
mail = [x for x in inbox("carol@client.example.com") if x["Subject"].startswith("Your sign-in link")][-1]
check(mail["Auto-Submitted"] == "auto-generated" and "expires in 15 minutes" in body(mail), "link email is automated and states expiry")
token1 = m.group(2)
anon.post(f"{B}/portal/api/request-link", json={"email": "carol@client.example.com"})
token2 = sign_in_link("carol@client.example.com", 1).group(2)
check(anon.post(f"{B}/portal/api/verify", json={"token": "x" * 43}).status_code == 400, "wrong token -> 400")
carol = requests.Session()
r = carol.post(f"{B}/portal/api/verify", json={"token": token2})
cookie = r.headers.get("set-cookie", "")
check(r.status_code == 200 and r.json()["email"] == "carol@client.example.com" and "Path=/portal" in cookie and "HttpOnly" in cookie,
      "verify signs in; cookie HttpOnly and scoped to /portal")
check(carol.post(f"{B}/portal/api/verify", json={"token": token2}).status_code == 400, "link works only once")
check(anon.post(f"{B}/portal/api/verify", json={"token": token1}).status_code == 400, "older unused link retired by signing in")
anon.post(f"{B}/portal/api/request-link", json={"email": "carol@client.example.com"})
token3 = sign_in_link("carol@client.example.com", 2).group(2)
psql("update customer_login_tokens set expires_at = now() - interval '1 minute' where used_at is null")
check(anon.post(f"{B}/portal/api/verify", json={"token": token3}).status_code == 400, "expired link -> 400")

# --- separation from agent sessions
check(carol.get(f"{B}/api/tickets").status_code == 401, "portal cookie gives no agent API access")
check(agent.get(f"{B}/portal/api/me").status_code == 401, "agent cookie gives no portal access")

# --- create ticket (multipart + attachment) -> agent side + emails
r = carol.post(f"{B}/portal/api/tickets", data={"name": "Carol C", "subject": "Printer <b>jam</b>", "message": "Tray 2 jams & beeps."},
               files=[("files", ("jam.png", b"\x89PNG\r\n\x1a\nportal", "image/png"))])
check(r.status_code == 201, f"create ticket -> 201 ({r.status_code} {r.text[:120]})")
t = r.json()
tid = t["id"]
check(t["status_label"] == "Received" and t["subject"] == "Printer <b>jam</b>" and t["messages"][0]["from_customer"]
      and t["messages"][0]["attachments"][0]["filename"] == "jam.png", "ticket view: status label, text as-is, attachment")
at = agent.get(f"{B}/api/tickets/{tid}").json()
check(at["source"] == "web" and at["requester_email"] == "carol@client.example.com" and at["requester_name"] == "Carol C"
      and at["priority"] == "medium", "agents see the portal ticket (source web, requester, medium)")
alert = wait_for(lambda: [x for x in inbox("alice@example.com") if f"[TICKET-{tid}]" in x["Subject"]])
check(alert and "New Ticket Created" in alert[0]["Subject"] and f"{B}/#/tickets/{tid}" in body(alert[0]), "agents alerted; link opens the dashboard ticket")
ack = wait_for(lambda: [x for x in inbox("carol@client.example.com") if f"[TICKET-{tid}]" in x["Subject"]])
check(ack and f"{B}/portal/#/tickets/{tid}" in body(ack[0]), "customer acknowledgement includes the portal link")
r = carol.post(f"{B}/portal/api/tickets", data={"subject": "URGENT: site down", "message": "Everything is down"})
check(r.status_code == 201 and agent.get(f"{B}/api/tickets/{r.json()['id']}").json()["priority"] == "urgent", "keyword escalation applies to portal tickets")

# --- agent activity: public reply is visible, internal things are not
agent.post(f"{B}/api/tickets/{tid}/comments", json={"body": "SECRET internal note", "is_internal": True}, headers={"Origin": B})
note_id = agent.get(f"{B}/api/tickets/{tid}").json()["comments"][-1]["id"]
psql(f"insert into ticket_attachments(ticket_id,comment_id,filename,content_type,size_bytes,sha256,storage_path) "
     f"select {tid},{note_id},'internal.txt','text/plain',1,sha256,storage_path from ticket_attachments where ticket_id={tid} limit 1")
internal_att = psql(f"select id from ticket_attachments where comment_id={note_id}")
agent.post(f"{B}/api/tickets/{tid}/comments", json={"body": "We've sent a technician.", "status": "pending"}, headers={"Origin": B})
v = carol.get(f"{B}/portal/api/tickets/{tid}").json()
texts = [m["body"] for m in v["messages"]]
check("We've sent a technician." in texts and not any("SECRET" in x for x in texts), "customer sees public reply, never the internal note")
check(not any("changed" in x or "Priority automatically" in x for x in texts), "audit/system lines hidden from customer")
check(v["status_label"] == "Awaiting your reply" and v["messages"][-1]["author_name"] == "Ada Admin"
      and not v["messages"][-1]["from_customer"], "status 'Awaiting your reply'; agent name shown")
own_att = v["messages"][0]["attachments"][0]["download_url"]
r = carol.get(B + own_att)
check(r.status_code == 200 and r.content == b"\x89PNG\r\n\x1a\nportal" and "attachment" in r.headers["content-disposition"], "own attachment downloads")
check(carol.get(f"{B}/portal/api/attachments/{internal_att}").status_code == 404, "attachment on an internal note -> 404")

# --- another customer sees nothing of Carol's
dave = customer("dave@client.example.com")
check(dave.get(f"{B}/portal/api/tickets").json() == [], "other customer's list is empty")
check(dave.get(f"{B}/portal/api/tickets/{tid}").status_code == 404, "other customer's ticket -> 404")
check(dave.get(B + own_att).status_code == 404, "other customer's attachment -> 404")
check(dave.post(f"{B}/portal/api/tickets/{tid}/messages", data={"message": "hi"}).status_code == 404, "can't reply to others' tickets")
check(dave.post(f"{B}/portal/api/tickets/{tid}/close").status_code == 404, "can't close others' tickets")

# --- reply (reopens pending), close, reply again (reopens resolved)
r = carol.post(f"{B}/portal/api/tickets/{tid}/messages", data={"message": "Technician fixed it, thanks!"},
               files=[("files", ("receipt.txt", b"ok", "text/plain"))])
check(r.status_code == 201 and r.json()["status"] == "open" and r.json()["messages"][-1]["attachments"][0]["filename"] == "receipt.txt",
      "customer reply with attachment reopens pending -> open")
last = agent.get(f"{B}/api/tickets/{tid}").json()["comments"][-1]
check(last["source"] == "web" and last["author"] is None and last["author_email"] == "carol@client.example.com", "agents see it as a portal customer message")
r = carol.post(f"{B}/portal/api/tickets/{tid}/close")
check(r.status_code == 200 and r.json()["status"] == "resolved" and not r.json()["is_open"], "'This is solved' resolves the ticket")
at = agent.get(f"{B}/api/tickets/{tid}").json()
check(at["resolved_at"] and "Customer marked the ticket as solved" in at["comments"][-1]["body"], "agents see who resolved it")
check(carol.post(f"{B}/portal/api/tickets/{tid}/close").json()["status"] == "resolved", "closing twice is harmless")
r = carol.post(f"{B}/portal/api/tickets/{tid}/messages", data={"message": "It broke again."})
check(r.json()["status"] == "open", "reply after solving reopens")

# --- email-created and imported tickets show up in the portal too
m = EmailMessage(); m["From"] = "carol@client.example.com"; m["To"] = "support@example.com"; m["Subject"] = "Emailed question"; m.set_content("via email")
with smtplib.SMTP("localhost", SMTP_PORT) as c:
    c.send_message(m)
emailed = wait_for(lambda: [x for x in carol.get(f"{B}/portal/api/tickets").json() if x["subject"] == "Emailed question"], 40)
check(emailed, "ticket created by email appears in the portal")
psql(f"update tickets set legacy_ref='ABC-DEF-1234' where id={emailed[0]['id']}")
check(carol.get(f"{B}/portal/api/tickets/{emailed[0]['id']}").json()["reference"] == "ABC-DEF-1234", "imported tickets show their HESK reference")

# --- validation + limits
check(carol.post(f"{B}/portal/api/tickets", data={"subject": " ", "message": "x"}).status_code == 400, "blank subject -> 400")
many = [("files", (f"f{i}.txt", b"x", "text/plain")) for i in range(6)]
r = carol.post(f"{B}/portal/api/tickets", data={"subject": "s", "message": "m"}, files=many)
check(r.status_code == 400 and "up to 5 files" in r.json()["error"], "more than 5 files -> 400")
big = [("files", ("big.bin", b"x" * (1024 * 1024 + 1), "application/octet-stream"))]
r = carol.post(f"{B}/portal/api/tickets/{tid}/messages", data={"message": "big"}, files=big)
check(r.status_code == 400 and "larger than 1 MB" in r.json()["error"], "oversized file -> 400")
codes = [carol.post(f"{B}/portal/api/tickets", data={"subject": f"t{i}", "message": "m"}).status_code for i in range(3)]
check(codes[-1] == 429, f"per-hour ticket limit (limit 4 in test config): {codes}")
codes = [anon.post(f"{B}/portal/api/request-link", json={"email": "erin@client.example.com"}).status_code for _ in range(6)]
check(codes[:5] == [202] * 5 and codes[5] == 429, "per-address link limit (5/hour)")
r = carol.post(f"{B}/portal/api/tickets/{tid}/messages", data={"message": "x"}, headers={"Origin": "https://evil.example"})
check(r.status_code == 403, "cross-site POST rejected")

# --- logout
r = carol.post(f"{B}/portal/api/logout")
check(r.status_code == 204 and carol.get(f"{B}/portal/api/me").status_code == 401, "logout ends the portal session")
check(agent.get(f"{B}/auth/me").status_code == 200, "agent session unaffected")

sys.exit(0 if ok else 1)
