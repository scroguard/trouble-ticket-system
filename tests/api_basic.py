"""Black-box API test: runs on the host against the compose stack (web :8000, GreenMail)."""
from common import BASE, COMPOSE, IMAP_PORT, REPO, SHOTS, SMTP_PORT, psql  # noqa: F401
import http.cookiejar, imaplib, json, smtplib, sys, time, urllib.error, urllib.request
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import make_msgid

BASE = BASE
ok = True


def check(cond, label):
    global ok
    print(("PASS " if cond else "FAIL ") + label)
    ok &= bool(cond)


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json"} if data else {}
        h.update(headers or {})
        r = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
        try:
            with self.opener.open(r) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw and resp.headers.get_content_type() == "application/json" else raw), resp.headers
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, (json.loads(raw) if raw else None), e.headers


def send_mail(frm, subject, text, headers=None, attach=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, "support@example.com", subject
    m["Message-ID"] = make_msgid(domain="client.test")
    for k, v in (headers or {}).items():
        m[k] = v
    m.set_content(text)
    if attach:
        m.add_attachment(attach[1], maintype="text", subtype="html", filename=attach[0])
    with smtplib.SMTP("localhost", SMTP_PORT) as s:
        s.send_message(m)
    return m["Message-ID"]


def inbox(user):
    c = imaplib.IMAP4("localhost", IMAP_PORT); c.login(user, "x"); c.select("INBOX")
    out = []
    for n in c.search(None, "ALL")[1][0].split():
        out.append(BytesParser(policy=policy.default).parsebytes(c.fetch(n, "(BODY.PEEK[])")[1][0][1]))
    c.logout()
    return out


def wait_for(fn, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if r := fn():
            return r
        time.sleep(0.5)
    return None


anon, admin, alice, bob = Client(), Client(), Client(), Client()

# --- auth & error format
s, b, _ = anon.req("GET", "/api/tickets")
check(s == 401 and b == {"error": "Authentication required"}, "unauthenticated /api -> 401 JSON")
s, b, _ = anon.req("GET", "/api/users")
check(s == 401, "unauthenticated /api/users -> 401")
s, b, _ = anon.req("GET", "/nope")
check(s == 404 and b == {"error": "Not Found"}, "unknown route -> 404 JSON")

s, b, _ = anon.req("POST", "/auth/register", {"email": "Ada@Example.com", "full_name": "Ada Admin", "password": "correct-horse-1", "role": "agent"})
check(s == 201 and b["role"] == "admin" and b["email"] == "ada@example.com", "first registration open, forced admin")
s, b, _ = anon.req("POST", "/auth/register", {"email": "x@example.com", "full_name": "X", "password": "correct-horse-1"})
check(s == 401, "second anonymous registration -> 401")

s, b, _ = anon.req("POST", "/auth/login", {"email": "ada@example.com", "password": "wrong-password"})
s2, b2, _ = anon.req("POST", "/auth/login", {"email": "ghost@example.com", "password": "wrong-password"})
check(s == s2 == 401 and b == b2 == {"error": "Invalid email or password"}, "bad password / unknown user indistinguishable")

s, b, h = admin.req("POST", "/auth/login", {"email": "ada@example.com", "password": "correct-horse-1"})
cookie = h.get("set-cookie", "")
check(s == 200 and b["access_token"] and "HttpOnly" in cookie and "SameSite=lax" in cookie, "login sets HttpOnly SameSite cookie")
admin_token = b["access_token"]
s, b, _ = admin.req("GET", "/auth/me")
check(s == 200 and b["email"] == "ada@example.com", "/auth/me via cookie")
s, b, _ = anon.req("GET", "/auth/me", headers={"Authorization": f"Bearer {admin_token}"})
check(s == 200, "/auth/me via Bearer token")

for email, name in [("alice@example.com", "Alice Agent"), ("bob@example.com", "Bob Agent")]:
    s, b, _ = admin.req("POST", "/auth/register", {"email": email, "full_name": name, "password": "agent-pass-123"})
    check(s == 201 and b["role"] == "agent", f"admin registers {name}")
s, b, _ = admin.req("POST", "/auth/register", {"email": "alice@example.com", "full_name": "Dup", "password": "agent-pass-123"})
check(s == 409 and "exists" in b["error"], "duplicate email -> 409")
s, b, _ = admin.req("POST", "/auth/register", {"email": "not-an-email", "full_name": "", "password": "short"})
check(s == 400 and b["error"] == "Validation failed" and {d["field"] for d in b["details"]} == {"email", "full_name", "password"}, "validation -> 400 with field details")

alice.req("POST", "/auth/login", {"email": "alice@example.com", "password": "agent-pass-123"})
bob.req("POST", "/auth/login", {"email": "bob@example.com", "password": "agent-pass-123"})
s, b, _ = alice.req("POST", "/auth/register", {"email": "eve@example.com", "full_name": "Eve", "password": "agent-pass-123"})
check(s == 403 and b == {"error": "Admin role required"}, "agent cannot register users -> 403")
s, users, _ = alice.req("GET", "/api/users?role=agent")
ids = {u["email"]: u["id"] for u in users}
check(s == 200 and set(ids) == {"alice@example.com", "bob@example.com"}, "GET /api/users?role=agent")

# --- ingest a ticket via email
mid = send_mail('"Carol" <carol@client.test>', "URGENT: printer on fire", "The printer is on fire.", attach=("evil.html", b"<script>alert(1)</script>"))
def find(q):
    s, b, _ = alice.req("GET", q)
    return b["items"] if s == 200 and b["items"] else None
items = wait_for(lambda: find("/api/tickets?status=new&assigned_to=none"))
check(items and items[0]["priority"] == "urgent" and items[0]["status"] == "new", "ingested ticket listed as new/unassigned/urgent")
tid = items[0]["id"]; tag = f"[TICKET-{tid}]"

s, d, _ = alice.req("GET", f"/api/tickets/{tid}")
check(s == 200 and d["tracking_code"] == f"TICKET-{tid}" and len(d["attachments"]) == 1 and d["comments"][0]["source"] == "system", "detail: attachments + chronological history")
s, raw, h = alice.req("GET", d["attachments"][0]["download_url"])
check(s == 200 and raw == b"<script>alert(1)</script>" and h["x-content-type-options"] == "nosniff"
      and "attachment" in h["content-disposition"] and h["content-security-policy"] == "sandbox", "attachment download is sandboxed")
s, b, _ = alice.req("GET", "/api/tickets/999999")
check(s == 404 and b == {"error": "Ticket not found"}, "missing ticket -> 404 JSON")
s, b, _ = alice.req("GET", "/api/attachments/999999")
check(s == 404, "missing attachment -> 404")

# --- PATCH validation
for body, label in [({}, "empty"), ({"status": "bogus"}, "bad status"), ({"priority": None}, "null priority"),
                    ({"assigned_to": 999999}, "unknown assignee"), ({"subject": "x"}, "unknown field")]:
    s, b, _ = alice.req("PATCH", f"/api/tickets/{tid}", body)
    check(s == 400 and b["error"] == "Validation failed" and b.get("details"), f"PATCH {label} -> 400")

# --- reassignment notification
s, b, _ = alice.req("PATCH", f"/api/tickets/{tid}", {"assigned_to": ids["bob@example.com"], "priority": "high"})
check(s == 200 and b["assignee"]["id"] == ids["bob@example.com"] and b["status"] == "open" and b["priority"] == "high",
      "PATCH assigns bob, bumps new->open, sets priority")
note = wait_for(lambda: [m for m in inbox("bob@example.com") if "Assigned to you" in m["Subject"] and tag in m["Subject"]])
check(note and note[0]["Subject"].startswith("[HIGH] Assigned to you:") and "by Alice Agent" in note[0].get_body(("plain",)).get_content(),
      "new assignee (bob) emailed with priority + who assigned")
s, b, _ = alice.req("PATCH", f"/api/tickets/{tid}", {"assigned_to": ids["bob@example.com"]})
time.sleep(2)
check(len([m for m in inbox("bob@example.com") if "Assigned to you" in m["Subject"]]) == 1, "unchanged assignee -> no duplicate email")
s, b, _ = alice.req("PATCH", f"/api/tickets/{tid}", {"assigned_to": ids["alice@example.com"]})
time.sleep(2)
check(s == 200 and not [m for m in inbox("alice@example.com") if "Assigned to you" in m["Subject"]], "self-assignment -> no email")

# --- claim
s, b, _ = bob.req("POST", f"/api/tickets/{tid}/claim")
check(s == 409 and "Alice Agent" in b["error"], "claim of someone else's ticket -> 409")
s, b, _ = alice.req("PATCH", f"/api/tickets/{tid}", {"assigned_to": None})
check(s == 200 and b["assignee"] is None, "assigned_to null unassigns")
s, b, _ = bob.req("POST", f"/api/tickets/{tid}/claim")
check(s == 200 and b["claimed"] and b["ticket"]["assignee"]["email"] == "bob@example.com", "claim unassigned ticket")

# --- comments
s, c, _ = bob.req("POST", f"/api/tickets/{tid}/comments", {"body": "Internal: fire dept called", "is_internal": True})
check(s == 201 and c["is_internal"] and c["delivery_status"] is None and c["author"]["email"] == "bob@example.com", "internal note created, not emailed")
s, b, _ = bob.req("POST", f"/api/tickets/{tid}/comments", {"body": "   "})
check(s == 400, "blank comment -> 400")
s, c, _ = bob.req("POST", f"/api/tickets/{tid}/comments", {"body": "We're on it. Please evacuate.", "status": "pending"})
check(s == 201 and not c["is_internal"] and c["delivery_status"] == "pending", "public reply accepted as pending delivery")
reply_id = c["id"]
def delivered():
    s, d, _ = bob.req("GET", f"/api/tickets/{tid}")
    return next((x for x in d["comments"] if x["id"] == reply_id and x["delivery_status"] == "sent"), None) and d
d = wait_for(delivered)
check(d and d["status"] == "pending", "reply delivered (status sent) and ticket set to pending")
got = wait_for(lambda: [m for m in inbox("carol@client.test") if "evacuate" in m.get_body(("plain",)).get_content()])
check(got and tag in got[0]["Subject"] and got[0]["In-Reply-To"] == mid, "customer received threaded reply")
cust = inbox("carol@client.test")
check(not any("fire dept" in m.get_body(("plain",)).get_content() for m in cust), "internal note never reached customer")
check([x["body"] for x in d["comments"] if x["source"] == "system" and "changed" in x["body"]], "audit trail of changes in history")

# customer answers -> pending ticket reopens
send_mail("carol@client.test", f"Re: {tag}", "It's out now, thanks", headers={"In-Reply-To": got[0]["Message-ID"]})
d = wait_for(lambda: (lambda d: d if d["status"] == "open" else None)(bob.req("GET", f"/api/tickets/{tid}")[1]))
check(d and d["comments"][-1]["body"] == "It's out now, thanks", "customer reply appended and ticket reopened")

# --- list filters
send_mail("dan@client.test", "Question about invoices", "hello")
wait_for(lambda: find("/api/tickets?q=invoices"))
s, b, _ = bob.req("GET", "/api/tickets?assigned_to=me")
check(s == 200 and [t["id"] for t in b["items"]] == [tid], "filter assigned_to=me")
s, b, _ = bob.req("GET", "/api/tickets?status=new&status=open&sort=-priority")
check(s == 200 and b["total"] == 2 and b["items"][0]["priority"] == "high", "multi-status filter + priority sort")
s, b, _ = bob.req("GET", "/api/tickets?status=resolved")
check(s == 200 and b["total"] == 0 and b["items"] == [], "status=resolved empty")
s, b, _ = bob.req("GET", "/api/tickets?q=printer")
check(s == 200 and b["total"] == 1, "full-text search")
s, b, _ = bob.req("GET", "/api/tickets?limit=1")
check(s == 200 and len(b["items"]) == 1 and b["total"] == 2, "pagination")
for q, label in [("?assigned_to=xyz", "bad assigned_to"), ("?status=Nope", "bad status"), ("?limit=0", "limit=0"), ("?sort=hax", "bad sort")]:
    s, b, _ = bob.req("GET", "/api/tickets" + q)
    check(s == 400 and b["error"] == "Validation failed", f"list {label} -> 400")

# --- CSRF origin check
s, b, _ = bob.req("POST", f"/api/tickets/{tid}/comments", {"body": "csrf", "is_internal": True}, headers={"Origin": "https://evil.example"})
check(s == 403 and b == {"error": "Cross-origin request rejected"}, "cross-origin write rejected")
s, b, _ = bob.req("POST", f"/api/tickets/{tid}/comments", {"body": "same origin", "is_internal": True}, headers={"Origin": BASE})
check(s == 201, "same-origin write allowed")

# --- logout revokes server-side
s, _, h = admin.req("POST", "/auth/logout")
check(s == 204 and "tts_session=" in h.get("set-cookie", ""), "logout 204 clears cookie")
s, _, _ = anon.req("GET", "/auth/me", headers={"Authorization": f"Bearer {admin_token}"})
check(s == 401, "token rejected after logout (server-side revocation)")

# --- lockout
statuses = [anon.req("POST", "/auth/login", {"email": "bob@example.com", "password": "nope-nope"})[0] for _ in range(5)]
s, b, _ = anon.req("POST", "/auth/login", {"email": "bob@example.com", "password": "agent-pass-123"})
check(statuses == [401] * 5 and s == 429 and b["details"]["retry_after"] > 0, "5 failures lock account (even correct pw -> 429)")

if __name__ == "__main__":
    sys.exit(0 if ok else 1)
