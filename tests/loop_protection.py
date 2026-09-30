"""Mail-loop protection: auto-responders (including ones that ignore every standard
header) must never keep a ticket <-> auto-reply loop going."""
import imaplib, smtplib, sys, time
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import requests

from common import BASE, IMAP_PORT, SMTP_PORT

B = BASE
C = "loopy@client.example.com"
ok = True


def check(cond, label):
    global ok
    print(("PASS " if cond else "FAIL ") + label, flush=True)
    ok &= bool(cond)


def send(subject, body, frm=C, headers=None, n=[0]):
    n[0] += 1
    m = EmailMessage(); m["From"] = frm; m["To"] = "support@example.com"; m["Subject"] = subject
    m["Message-ID"] = f"<loop-{n[0]}-{time.time()}@client.example.com>"
    for k, v in (headers or {}).items():
        m[k] = v
    m.set_content(body)
    with smtplib.SMTP("localhost", SMTP_PORT) as c:
        c.send_message(m)


def inbox(user):
    c = imaplib.IMAP4("localhost", IMAP_PORT); c.login(user, "x"); c.select("INBOX")
    out = [BytesParser(policy=policy.default).parsebytes(c.fetch(i, "(BODY.PEEK[])")[1][0][1]) for i in c.search(None, "ALL")[1][0].split()]
    c.logout()
    return out


def acks(user):
    return [m for m in inbox(user) if "TICKET-" in m["Subject"] and "received your request" in m.get_body(("plain",)).get_content()]


def wait(fn, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        if r := fn():
            return r
        time.sleep(1)
    return None


requests.post(f"{B}/auth/register", json={"email": "ada@example.com", "full_name": "Ada Admin", "password": "correct-horse-1"})
s = requests.Session(); s.post(f"{B}/auth/login", json={"email": "ada@example.com", "password": "correct-horse-1"})
total = lambda: s.get(f"{B}/api/tickets").json()["total"]

send("Printer help", "Please help")
ack = wait(lambda: acks(C))
check(ack and ack[0]["X-Loop"] == "support@example.com" and ack[0]["Auto-Submitted"] == "auto-generated",
      "acknowledgement carries X-Loop + Auto-Submitted")
tid = s.get(f"{B}/api/tickets").json()["items"][0]["id"]

# 1) auto-responder with no auto headers and a generic subject, but replying to our ack
for i in range(3):
    send("Thanks for your email", f"We got it ({i}).", headers={"In-Reply-To": ack[0]["Message-ID"]})
wait(lambda: len([c for c in s.get(f"{B}/api/tickets/{tid}").json()["comments"] if c["body"].startswith("We got it")]) == 3)
check(total() == 1, "replies to our acknowledgement thread onto the ticket (no new tickets)")
check(len(acks(C)) == 1, "...and trigger no further acknowledgements")

# 2) worst case: header-less, unthreadable auto-replies -> hourly acknowledgement cap
for i in range(5):
    send(f"We received your message {i}", "Generic auto text.")
    time.sleep(6)
wait(lambda: total() == 6, 60)
time.sleep(8)
check(len(acks(C)) == 3, f"acknowledgements capped at 3 per address per hour ({len(acks(C))})")

# 3) recognised auto-replies/bounces are ignored entirely
before = total()
send("Automatic reply: Printer help", "I'm away until Monday")
send("Out of Office: Re: [TICKET-%d] Printer help" % tid, "Away")
send("Undeliverable: Printer help", "bounce", frm="mailer-daemon@client.example.com")
send("fwd", "our own mail came back", headers={"X-Loop": "support@example.com"})
send("Re: something", "exchange oof", headers={"X-Auto-Response-Suppress": "All"})
time.sleep(20)
check(total() == before, "auto-replies, bounces, Exchange OOF and looped-back mail are ignored")
check(not any(c["body"] == "Away" for c in s.get(f"{B}/api/tickets/{tid}").json()["comments"]), "...even when they reference a ticket")

# 4) no-reply senders: ticket yes, acknowledgement no
send("Your order has shipped", "Tracking 123", frm="no-reply@shop.example.com")
wait(lambda: total() == before + 1)
time.sleep(8)
check(total() == before + 1 and not inbox("no-reply@shop.example.com"), "no-reply sender: ticket created, no acknowledgement sent")

# 5) a genuine customer message is not mistaken for an auto-reply
send("Autoloader jammed on the office printer", "Real problem, please help.", frm="real@client.example.com")
check(wait(lambda: total() == before + 2), "normal mail with lookalike words still becomes a ticket")

sys.exit(0 if ok else 1)
