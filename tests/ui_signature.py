"""Agent signatures: API + email + portal + dashboard UI. Fresh stack seeded with seed.py."""
from common import BASE, COMPOSE, IMAP_PORT, REPO, SHOTS, SMTP_PORT, psql  # noqa: F401
import imaplib, sys, time
from email import policy
from email.parser import BytesParser
from playwright.sync_api import sync_playwright, expect

B = BASE
ok = True
problems = []


def check(cond, label):
    global ok
    print(("PASS " if cond else "FAIL ") + label, flush=True)
    ok &= bool(cond)


def emails_to(addr, containing):
    end = time.time() + 30
    while time.time() < end:
        c = imaplib.IMAP4("localhost", IMAP_PORT); c.login(addr, "x"); c.select("INBOX")
        msgs = [BytesParser(policy=policy.default).parsebytes(c.fetch(i, "(BODY.PEEK[])")[1][0][1]) for i in c.search(None, "ALL")[1][0].split()]
        c.logout()
        bodies = [m.get_body(("plain",)).get_content().replace("\r\n", "\n") for m in msgs]  # email uses CRLF
        hits = [b for b in bodies if containing in b]
        if hits:
            return hits
        time.sleep(0.5)
    return []


with sync_playwright() as p:
    browser = p.chromium.launch()
    expect.set_options(timeout=15000)
    ada = browser.new_context().request
    ada.post(f"{B}/auth/login", data={"email": "ada@example.com", "password": "correct-horse-1"})
    alice = browser.new_context().request
    alice.post(f"{B}/auth/login", data={"email": "alice@example.com", "password": "agent-pass-123"})
    H = {"Origin": B}

    # --- profile API
    check(ada.get(f"{B}/auth/me").json()["signature"] is None, "no signature by default")
    r = ada.patch(f"{B}/auth/me", data={"signature": "Thank you,\r\nAda Admin   \nSupport Lead\n\n"}, headers=H)
    check(r.status == 200 and r.json()["signature"] == "Thank you,\nAda Admin\nSupport Lead", "save signature (normalized line endings/whitespace)")
    check(ada.patch(f"{B}/auth/me", data={"signature": "x" * 2001}, headers=H).status == 400, "over 2000 characters -> 400")
    check(ada.patch(f"{B}/auth/me", data={"role": "admin"}, headers=H).status == 400, "can't change anything else via /auth/me")

    import smtplib
    from email.message import EmailMessage
    m = EmailMessage(); m["From"] = "Gail <gail@client.example.com>"; m["To"] = "support@example.com"
    m["Subject"] = "Scanner offline"; m.set_content("Our scanner went offline.")
    with smtplib.SMTP("localhost", SMTP_PORT) as c:
        c.send_message(m)
    end = time.time() + 40
    while time.time() < end and not (items := ada.get(f"{B}/api/tickets?q=scanner").json()["items"]):
        time.sleep(1)
    tid = items[0]["id"]
    customer = items[0]["requester_email"]

    # --- public reply gets the signature, stored and emailed; no automatic name line
    r = ada.post(f"{B}/api/tickets/{tid}/comments", data={"body": "We found the cause."}, headers=H)
    check(r.json()["body"] == "We found the cause.\n\nThank you,\nAda Admin\nSupport Lead", "reply body includes the signature")
    mail = emails_to(customer, "We found the cause.")
    check(mail and "Support Lead" in mail[0] and "-- \nAda Admin" not in mail[0] and mail[0].count("Thank you,") == 1,
          "emailed once, without the automatic '-- name' line")

    r = ada.post(f"{B}/api/tickets/{tid}/comments", data={"body": "Quick follow-up.", "include_signature": False}, headers=H)
    check(r.json()["body"] == "Quick follow-up.", "include_signature=false leaves it off")
    r = ada.post(f"{B}/api/tickets/{tid}/comments", data={"body": "note to self", "is_internal": True}, headers=H)
    check(r.json()["body"] == "note to self", "internal notes never get the signature")

    # --- agent without a signature keeps the automatic name line
    r = alice.post(f"{B}/api/tickets/{tid}/comments", data={"body": "Alice here."}, headers=H)
    check(r.json()["body"] == "Alice here.", "no signature -> body unchanged")
    mail = emails_to(customer, "Alice here.")
    check(mail and "-- \nAlice Agent" in mail[0], "no signature -> automatic name line in the email")

    # --- dashboard UI
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.on("pageerror", lambda e: problems.append(str(e)))
    page.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text and problems.append(m.text))
    page.goto(B + "/")
    page.fill("#login-email", "alice@example.com"); page.fill("#login-password", "agent-pass-123"); page.click("#login-submit")
    page.goto(f"{B}/#/tickets/{tid}")
    expect(page.locator("#t-subject")).to_be_visible()
    check(not page.locator("#signature-row").is_visible(), "no signature -> no signature option in the reply box")
    page.click(".user-menu-btn"); page.click("#signature-open")
    expect(page.locator("#signature-text")).to_be_focused()
    page.fill("#signature-text", "Best regards,\nAlice Agent\nTechnical Support")
    page.click("#signature-save")
    expect(page.locator(".toast", has_text="Signature saved")).to_be_visible()
    expect(page.locator("#signature-row")).to_be_visible()
    check(page.locator("#reply-signature").is_checked() and
          page.locator("#signature-preview").inner_text() == "Best regards, · Alice Agent · Technical Support", "checkbox on by default; one-line preview")
    page.click("label[for=mode-internal]")
    check(not page.locator("#signature-row").is_visible(), "hidden for internal notes")
    page.click("label[for=mode-public]")
    page.fill("#reply-body", "Replacement is on the way.")
    page.click("#reply-submit")
    msg = page.locator(".msg-agent", has_text="Replacement is on the way.")
    expect(msg).to_be_visible()
    check("Best regards,\nAlice Agent\nTechnical Support" in msg.locator(".msg-body").inner_text(), "sent reply shows the signature in the timeline")
    page.screenshot(path=f"{SHOTS}/signature.png")
    page.uncheck("#reply-signature")
    page.fill("#reply-body", "One more thing.")
    page.click("#reply-submit")
    msg = page.locator(".msg-agent", has_text="One more thing.")
    expect(msg).to_be_visible()
    check(msg.locator(".msg-body").inner_text().strip() == "One more thing." and page.locator("#reply-signature").is_checked(),
          "unticked -> no signature; checkbox resets for the next reply")

    # --- the customer sees the same text in the portal
    import re
    ada.post(f"{B}/portal/api/request-link", data={"email": customer})
    link = None
    end = time.time() + 30
    while time.time() < end and not link:
        c = imaplib.IMAP4("localhost", IMAP_PORT); c.login(customer, "x"); c.select("INBOX")
        for i in c.search(None, "ALL")[1][0].split():
            m = BytesParser(policy=policy.default).parsebytes(c.fetch(i, "(BODY.PEEK[])")[1][0][1])
            if m["Subject"].startswith("Your sign-in link"):
                link = re.search(r"#/verify/([\w-]+)", m.get_body(("plain",)).get_content()).group(1)
        c.logout(); time.sleep(0.5)
    cust = browser.new_context().request
    cust.post(f"{B}/portal/api/verify", data={"token": link})
    bodies = [m["body"] for m in cust.get(f"{B}/portal/api/tickets/{tid}").json()["messages"]]
    check("Replacement is on the way.\n\nBest regards,\nAlice Agent\nTechnical Support" in bodies and "note to self" not in bodies,
          "portal shows the signed reply (and never the note)")

    # --- clearing
    page.click(".user-menu-btn"); page.click("#signature-open")
    page.fill("#signature-text", "   ")
    page.click("#signature-save")
    expect(page.locator(".toast", has_text="Signature removed")).to_be_visible()
    check(not page.locator("#signature-row").is_visible(), "clearing removes the option")

    check(not problems, "no console errors")
    for pr in problems:
        print("   ", pr)
    browser.close()

sys.exit(0 if ok else 1)
