"""Drive the dashboard in headless Chromium. Screenshots go to /out."""
from common import BASE, COMPOSE, IMAP_PORT, REPO, SHOTS, SMTP_PORT, psql  # noqa: F401
import re, sys, time
from playwright.sync_api import sync_playwright, expect

B = BASE
ok = True
problems = []


def check(cond, label):
    global ok
    print(("PASS " if cond else "FAIL ") + label, flush=True)
    ok &= bool(cond)


with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    # Chrome logs every non-2xx fetch as "Failed to load resource"; those are expected
    # here (401 before login, bad password, mocked 400). Anything else, including
    # CSP "Refused to ..." violations, is a real problem.
    page.on("console", lambda m: m.type in ("error", "warning") and "Failed to load resource" not in m.text
            and problems.append(f"console.{m.type}: {m.text}"))
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    expect.set_options(timeout=15000)

    # --- login
    page.goto(B + "/")
    expect(page.locator("#login-view")).to_be_visible()
    page.fill("#login-email", "alice@example.com")
    page.fill("#login-password", "wrong-password")
    page.click("#login-submit")
    expect(page.locator("#login-error")).to_have_text("Invalid email or password")
    check(True, "bad login shows API error")
    page.fill("#login-password", "agent-pass-123")
    page.click("#login-submit")
    expect(page.locator("#app-view")).to_be_visible()
    expect(page.locator(".ticket-card")).to_have_count(4)
    check(True, "login -> dashboard with 4 tickets")
    expect(page.locator('[data-filter="all"] .tab-count')).to_have_text("4")
    expect(page.locator('[data-filter="new"] .tab-count')).to_have_text("4")
    check(True, "tab counts")

    urgent = page.locator(".ticket-card", has_text="payment server down")
    check("prio-urgent" in urgent.get_attribute("class"), "urgent card has red accent class")
    check(urgent.locator(".badge.pr-urgent").inner_text().strip() == "Urgent", "urgent priority badge")
    check(urgent.locator(".badge.st-new").inner_text().strip() == "New", "status badge")
    check("carol@client.test" in urgent.inner_text() and "Unassigned" in urgent.inner_text(), "card shows sender + assignee")
    low = page.locator(".ticket-card", has_text="Password reset")
    check(low.locator(".badge.pr-low").count() == 1, "low priority (sender Importance: low) gray badge")
    long_subject = page.locator(".ticket-card .t-subject", has_text="invoice")
    check(long_subject.evaluate("e => e.scrollWidth > e.clientWidth"), "long subject truncated with ellipsis")
    page.screenshot(path=f"{SHOTS}/1-list.png")

    # --- open ticket
    urgent.click()
    expect(page.locator("#t-subject")).to_have_text("URGENT: payment server down since 9am")
    check(re.search(r"#/tickets/\d+$", page.url) is not None, "URL hash deep-links the ticket")
    tid = int(page.url.rsplit("/", 1)[1])
    expect(page.locator("#t-code")).to_have_text(f"TICKET-{tid}")
    body = page.locator(".msg-original .msg-body").inner_text()
    check("<script>alert('xss')</script>" in body, "customer HTML-ish text rendered as inert text")
    check(page.locator(".msg-original .attachment").get_attribute("href").startswith("/api/attachments/"), "attachment link")
    check(page.locator(".msg-system", has_text="Priority automatically set to Urgent").count() == 1, "escalation audit note in timeline")
    check(page.locator("#t-assignee").input_value() == "", "assignee dropdown shows Unassigned")
    page.screenshot(path=f"{SHOTS}/2-ticket.png")

    # --- assign to Bob via dropdown
    bob_id = page.locator("#t-assignee option", has_text="Bob Agent").get_attribute("value")
    page.select_option("#t-assignee", bob_id)
    expect(page.locator(".toast", has_text="Assigned to Bob Agent")).to_be_visible()
    expect(page.locator("#t-status")).to_have_value("open")
    expect(page.locator("#t-status-badge .badge")).to_have_text("Open")
    expect(urgent.locator(".t-assignee")).to_contain_text("Bob Agent")
    check(True, "assign via dropdown: toast, status auto new->open, card updated")
    expect(page.locator(".msg-system", has_text="assignee nobody → Bob Agent")).to_be_visible()
    check(True, "audit entry appears in timeline")

    # --- priority + status dropdowns
    page.select_option("#t-priority", "high")
    expect(page.locator("#t-priority-badge .badge")).to_have_text("High")
    check(True, "priority change")

    # --- internal note
    page.click("label[for=mode-internal]")
    check("is-internal" in page.locator("#reply-form").get_attribute("class"), "internal mode restyles reply box")
    page.fill("#reply-body", "Checked the logs: LB health check failing.")
    page.click("#reply-submit")
    expect(page.locator(".msg-note", has_text="LB health check failing")).to_be_visible()
    check(page.locator("#reply-body").input_value() == "", "reply box cleared after submit")

    # --- public reply with status -> pending, delivery tracking
    page.click("label[for=mode-public]")
    page.fill("#reply-body", "We're on it, a fix is rolling out now.")
    page.select_option("#reply-status", "pending")
    page.keyboard.press("Control+Enter")
    agent_msg = page.locator(".msg-agent", has_text="fix is rolling out")
    expect(agent_msg).to_be_visible()
    expect(agent_msg.locator(".delivery")).to_contain_text("Emailed to customer", timeout=20000)
    check(True, "public reply via Ctrl+Enter; delivery status updates to Emailed")
    expect(page.locator("#t-status-badge .badge")).to_have_text("Pending")
    check(True, "reply set status to Pending")
    time.sleep(0.8)
    at_bottom = page.locator("#timeline").evaluate("e => e.scrollHeight - e.scrollTop - e.clientHeight < 5")
    check(at_bottom, "timeline scrolled to bottom after reply")
    bg_agent = agent_msg.locator(".msg-bubble").evaluate("e => getComputedStyle(e).backgroundColor")
    bg_customer = page.locator(".msg-original .msg-bubble").evaluate("e => getComputedStyle(e).backgroundColor")
    bg_note = page.locator(".msg-note .msg-bubble").first.evaluate("e => getComputedStyle(e).backgroundColor")
    check(len({bg_agent, bg_customer, bg_note}) == 3, f"distinct backgrounds agent/customer/note {bg_agent} {bg_customer} {bg_note}")
    page.screenshot(path=f"{SHOTS}/3-conversation.png")

    # --- drafts survive switching tickets
    page.fill("#reply-body", "half-written draft")
    page.locator(".ticket-card", has_text="Feature request").click()
    expect(page.locator("#t-subject")).to_have_text("Feature request: dark mode")
    check(page.locator("#reply-body").input_value() == "", "other ticket has empty reply box")
    page.go_back()
    expect(page.locator("#t-subject")).to_contain_text("payment server down")
    check(page.locator("#reply-body").input_value() == "half-written draft", "browser Back returns to ticket with draft kept")

    # --- filters
    page.click('[data-filter="pending"]')
    expect(page.locator(".ticket-card")).to_have_count(1)
    page.click('[data-filter="new"]')
    expect(page.locator(".ticket-card")).to_have_count(3)
    page.click('[data-filter="resolved"]')
    expect(page.locator(".list-empty")).to_be_visible()
    page.click('[data-filter="all"]')
    expect(page.locator(".ticket-card")).to_have_count(4)
    check(True, "status tabs filter the list (pending 1, new 3, resolved 0, all 4)")
    page.select_option("#assigned-filter", "none")
    expect(page.locator(".ticket-card")).to_have_count(3)
    page.select_option("#assigned-filter", "")
    page.fill("#search", "invoice")
    expect(page.locator(".ticket-card")).to_have_count(1)
    page.fill("#search", "")
    expect(page.locator(".ticket-card")).to_have_count(4)
    check(True, "assignee filter + search")

    # --- new email arrives -> manual refresh picks it up
    page.uncheck("#auto-refresh")  # test drives refreshes itself (no racing the 30s timer)
    check(page.evaluate("state.pollTimer") is None, "auto-refresh switch stops polling")
    import smtplib
    from email.message import EmailMessage
    m = EmailMessage(); m["From"] = "gus@client.test"; m["To"] = "support@example.com"; m["Subject"] = "Brand new ticket from Gus"; m.set_content("hi")
    with smtplib.SMTP("localhost", SMTP_PORT) as s:
        s.send_message(m)
    time.sleep(10)
    page.click("#refresh-btn")
    expect(page.locator(".ticket-card", has_text="Brand new ticket from Gus")).to_be_visible()
    expect(page.locator(".toast", has_text="new ticket")).to_have_count(0)  # manual refresh isn't a "quiet" poll
    check(True, "Refresh button shows newly ingested ticket")
    page.evaluate("refreshList({ quiet: true })")  # what the 30s poll calls
    m.replace_header("Subject", "Second arrival via poll"); del m["Message-ID"]
    with smtplib.SMTP("localhost", SMTP_PORT) as s:
        s.send_message(m)
    time.sleep(10)
    page.evaluate("refreshList({ quiet: true })")
    expect(page.locator(".toast", has_text="1 new ticket in this view")).to_be_visible()
    expect(page.locator(".ticket-card.is-new-arrival", has_text="Second arrival")).to_be_visible()
    check(True, "background poll announces + highlights new arrivals")

    # --- error path: API validation error surfaces as toast and dropdown reverts
    page.locator(".ticket-card", has_text="payment server down").click()
    expect(page.locator("#t-subject")).to_contain_text("payment server down")
    page.route("**/api/tickets/*", lambda route: route.fulfill(status=400, content_type="application/json",
        body='{"error":"Validation failed","details":[{"field":"status","message":"nope"}]}') if route.request.method == "PATCH" else route.continue_())
    page.select_option("#t-status", "closed")
    expect(page.locator(".toast", has_text="Update failed: status: nope")).to_be_visible()
    expect(page.locator("#t-status")).to_have_value("pending")
    check(True, "failed PATCH shows error toast and reverts dropdown")
    page.unroute("**/api/tickets/*")

    # --- dark mode
    page.click(".user-menu-btn"); page.click("#theme-toggle")
    check(page.evaluate("document.documentElement.dataset.bsTheme") == "dark", "dark mode toggle")
    for b in page.locator(".toast .btn-close").all():
        b.click()
    time.sleep(0.6)  # let colour transitions finish
    page.screenshot(path=f"{SHOTS}/4-dark.png")
    page.click(".user-menu-btn"); page.click("#theme-toggle")

    # --- mobile layout
    mob = browser.new_context(viewport={"width": 390, "height": 844}, storage_state=ctx.storage_state())
    mp = mob.new_page()
    mp.on("pageerror", lambda e: problems.append(f"mobile pageerror: {e}"))
    mp.goto(B + "/")
    expect(mp.locator(".ticket-card").first).to_be_visible()
    check(not mp.locator("#workspace").is_visible(), "mobile: list only")
    mp.screenshot(path=f"{SHOTS}/5-mobile-list.png")
    mp.locator(".ticket-card", has_text="payment server down").click()
    expect(mp.locator("#t-subject")).to_be_visible()
    check(not mp.locator("#list-pane").is_visible(), "mobile: ticket replaces list")
    check(mp.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "mobile: no horizontal scroll")
    tl = mp.locator("#timeline").bounding_box()
    check(tl["height"] >= 844 * 0.35, f"mobile: conversation gets >=35% of the screen ({tl['height']:.0f}px)")
    mp.screenshot(path=f"{SHOTS}/6-mobile-ticket.png")
    mp.click("#back-btn")
    expect(mp.locator("#list-pane")).to_be_visible()
    check(True, "mobile: back button returns to list")

    # --- logout & session end
    page.click(".user-menu-btn"); page.click("#logout-btn")
    expect(page.locator("#login-view")).to_be_visible()
    page.reload()
    expect(page.locator("#login-view")).to_be_visible()
    check(True, "logout returns to login (and survives reload)")

    check(not problems, "no console errors / CSP violations / page errors")
    for pr in problems:
        print("   ", pr)
    browser.close()

sys.exit(0 if ok else 1)
