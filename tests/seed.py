"""Seed a fresh stack: admin + 2 agents, several customer emails."""
from common import BASE, COMPOSE, IMAP_PORT, REPO, SHOTS, SMTP_PORT, psql  # noqa: F401
import json, smtplib, time, urllib.request
from email.message import EmailMessage
from email.utils import make_msgid
B = BASE
def post(path, body, cookie=None):
    r = urllib.request.Request(B + path, json.dumps(body).encode(), {"Content-Type": "application/json", **({"Cookie": cookie} if cookie else {})})
    with urllib.request.urlopen(r) as resp:
        return resp.headers.get("set-cookie", "").split(";")[0]
post("/auth/register", {"email": "ada@example.com", "full_name": "Ada Admin", "password": "correct-horse-1"})
c = post("/auth/login", {"email": "ada@example.com", "password": "correct-horse-1"})
for e, n in [("alice@example.com", "Alice Agent"), ("bob@example.com", "Bob Agent")]:
    post("/auth/register", {"email": e, "full_name": n, "password": "agent-pass-123"}, c)
def mail(frm, subj, text, headers=None, attach=None):
    m = EmailMessage(); m["From"], m["To"], m["Subject"] = frm, "support@example.com", subj
    m["Message-ID"] = make_msgid(domain="client.test")
    for k, v in (headers or {}).items(): m[k] = v
    m.set_content(text)
    if attach: m.add_attachment(attach[1], maintype="application", subtype="pdf", filename=attach[0])
    with smtplib.SMTP("localhost", SMTP_PORT) as s: s.send_message(m)
    return m["Message-ID"]
mail('"Carol Customer" <carol@client.test>', "URGENT: payment server down since 9am", "Hi team,\n\nOur checkout has been failing since 9am. Customers see a 502 error.\n<script>alert('xss')</script>\n\nThanks,\nCarol", attach=("error-log.pdf", b"%PDF-1.4 fake"))
mail('"Dan Ortiz" <dan@client.test>', "Question about our invoice for September and a much longer subject line that should truncate", "Hello, the invoice total looks off by $20. Can you check?")
mail("erin@client.test", "Password reset link not arriving", "I requested a reset link three times, nothing arrives.", headers={"Importance": "low"})
mail('"Fay" <fay@client.test>', "Feature request: dark mode", "Would love a dark mode in the app.")
time.sleep(12)
print("seeded")
