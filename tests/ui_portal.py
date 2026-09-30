"""Customer portal in headless Chromium (fresh stack)."""
from common import BASE, COMPOSE, IMAP_PORT, REPO, SHOTS, SMTP_PORT, psql  # noqa: F401
import imaplib, re, sys, time
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


def link_for(email, n_before):
    end = time.time() + 30
    while time.time() < end:
        c = imaplib.IMAP4("localhost", IMAP_PORT); c.login(email, "x"); c.select("INBOX")
        msgs = [BytesParser(policy=policy.default).parsebytes(c.fetch(i, "(BODY.PEEK[])")[1][0][1]) for i in c.search(None, "ALL")[1][0].split()]
        c.logout()
        links = [m for m in msgs if m["Subject"].startswith("Your sign-in link")]
        if len(links) > n_before:
            return re.search(r"https?://\S+/portal/#/verify/[\w-]+", links[-1].get_body(("plain",)).get_content()).group(0)
        time.sleep(0.5)
    return None


with sync_playwright() as p:
    browser = p.chromium.launch()
    expect.set_options(timeout=15000)
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.on("console", lambda m: m.type in ("error", "warning") and "Failed to load resource" not in m.text and problems.append(m.text))
    page.on("pageerror", lambda e: problems.append(str(e)))
    page.on("dialog", lambda d: d.accept())

    # agent setup through the API
    api = ctx.request
    api.post(f"{B}/auth/register", data={"email": "ada@example.com", "full_name": "Ada Admin", "password": "correct-horse-1"})

    # --- signed out, arriving from a "view this ticket online" link
    page.goto(f"{B}/portal/#/tickets/424242")
    expect(page.locator("#signin-email")).to_be_visible()
    check(page.title() == "Support Desk" and page.locator("#brand-name").inner_text() == "Support Desk", "portal shows the site name")
    page.click("button[type=submit]")
    expect(page.locator(".alert-danger")).to_have_text("Enter a valid email address.")
    page.fill("#signin-email", "frank@client.example.com")
    page.click("button[type=submit]")
    expect(page.get_by_text("Check your email")).to_be_visible()
    check(True, "request link -> 'Check your email'")
    page.screenshot(path=f"{SHOTS}/p1-check-email.png")

    link = link_for("frank@client.example.com", 0)
    check(link is not None, "sign-in link arrives by email")
    page.goto(link)
    expect(page.get_by_role("button", name="Continue to sign in")).to_be_visible()
    check(True, "link opens a Continue page (link scanners can't sign in)")
    page.get_by_role("button", name="Continue to sign in").click()
    expect(page.get_by_text("We couldn't find that ticket in your account.")).to_be_visible()
    check(page.url.endswith("#/tickets/424242"), "after sign-in, returns to the page the customer was heading to")
    check("verify" not in page.url, "token removed from the address bar")
    expect(page.locator("#account-email")).to_have_text("frank@client.example.com")

    # --- empty list -> new request with attachment
    page.goto(f"{B}/portal/#/")
    expect(page.get_by_text("You don't have any tickets yet.")).to_be_visible()
    page.get_by_role("link", name="New request").click()
    page.fill("#new-name", "Frank")
    page.click("button[type=submit]")
    expect(page.locator(".alert-danger")).to_contain_text("fill in the subject")
    page.fill("#new-subject", "VPN keeps dropping")
    page.fill("#new-message", "Every 10 minutes the VPN disconnects.\n<script>alert(1)</script>")
    page.set_input_files("#new-files", files=[{"name": "log.txt", "mimeType": "text/plain", "buffer": b"disconnect at 10:02"}])
    page.click("button[type=submit]")
    expect(page.locator("h1", has_text="VPN keeps dropping")).to_be_visible()
    expect(page.locator(".toast", has_text="Request received")).to_be_visible()
    check(page.locator(".status").inner_text() == "Received", "new ticket view: status 'Received'")
    first = page.locator(".bubble-row").first
    check("<script>alert(1)</script>" in first.inner_text() and first.locator(".file-chip", has_text="log.txt").count() == 1,
          "message shown as text; attachment listed")
    tid = int(page.url.rsplit("/", 1)[1])
    check("null" not in page.locator("#view").inner_text() and "[object" not in page.locator("#view").inner_text(), "no stray 'null'/'[object' text")

    # --- agent answers (public) and adds an internal note
    agent = browser.new_context().request
    agent.post(f"{B}/auth/login", data={"email": "ada@example.com", "password": "correct-horse-1"})
    agent.post(f"{B}/api/tickets/{tid}/comments", data={"body": "Internal: check firewall", "is_internal": True}, headers={"Origin": B})
    agent.post(f"{B}/api/tickets/{tid}/comments", data={"body": "Please update your VPN client to 5.2.", "status": "pending"}, headers={"Origin": B})
    page.reload()
    expect(page.locator(".bubble-row.support", has_text="update your VPN client")).to_be_visible()
    check(page.get_by_text("Internal: check firewall").count() == 0, "internal note not shown to the customer")
    check(page.locator(".status").inner_text() == "Awaiting your reply", "status 'Awaiting your reply'")
    check(page.locator(".bubble-row.support .who").inner_text() == "Ada Admin", "agent reply shows the agent's name")

    # --- reply with attachment
    page.fill("#reply-message", "Updated, still drops.")
    page.set_input_files("#reply-files", files=[{"name": "screen.png", "mimeType": "image/png", "buffer": b"\x89PNG\r\n\x1a\nui"}])
    page.click("form button[type=submit]")
    expect(page.locator(".bubble-row.mine", has_text="Updated, still drops.")).to_be_visible()
    check(page.locator(".bubble-row.mine").last.locator(".file-chip", has_text="screen.png").count() == 1, "reply with attachment")
    check(page.locator(".status").inner_text() == "In progress", "reply moves it back to 'In progress'")
    time.sleep(0.6)
    page.screenshot(path=f"{SHOTS}/p2-ticket.png", full_page=True)

    # --- mark solved
    page.get_by_role("button", name="This is solved").click()
    expect(page.locator(".status")).to_have_text("Resolved")
    expect(page.get_by_text("This ticket is resolved.")).to_be_visible()
    check(page.get_by_role("button", name="This is solved").count() == 0, "'This is solved' resolves; banner explains reopening")

    # --- list
    page.goto(f"{B}/portal/#/")
    expect(page.locator(".ticket-row")).to_have_count(1)
    check("resolved" in page.locator("h2").inner_text().lower() and "#TICKET-" in page.locator(".ticket-row .meta").inner_text(), "list groups resolved tickets")
    page.screenshot(path=f"{SHOTS}/p3-list.png")

    # --- agent dashboard labels portal messages
    dash = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
    dash.goto(B + "/")
    dash.fill("#login-email", "ada@example.com"); dash.fill("#login-password", "correct-horse-1"); dash.click("#login-submit")
    dash.goto(f"{B}/#/tickets/{tid}")
    expect(dash.locator(".msg-original .badge", has_text="Submitted via portal")).to_be_visible()
    expect(dash.locator(".badge", has_text="Customer (portal)")).to_be_visible()
    check(True, "agent dashboard marks portal submissions and replies")
    dash.screenshot(path=f"{SHOTS}/p4-dashboard.png")

    # --- mobile + dark
    mob = browser.new_context(viewport={"width": 390, "height": 844}, storage_state=ctx.storage_state(), color_scheme="dark")
    mp = mob.new_page()
    mp.on("pageerror", lambda e: problems.append(str(e)))
    mp.goto(f"{B}/portal/#/tickets/{tid}")
    expect(mp.locator(".bubble-row").first).to_be_visible()
    check(mp.evaluate("document.documentElement.dataset.bsTheme") == "dark", "follows dark mode")
    check(mp.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "mobile: no horizontal scroll")
    mp.screenshot(path=f"{SHOTS}/p5-mobile.png", full_page=True)

    # --- sign out
    page.click("#logout")
    expect(page.get_by_text("You're signed out.")).to_be_visible()
    page.reload()
    expect(page.locator("#signin-email")).to_be_visible()
    check(True, "sign out returns to sign-in (and stays signed out)")

    check(not problems, "no console errors / CSP violations")
    for pr in problems:
        print("   ", pr)
    browser.close()

sys.exit(0 if ok else 1)
