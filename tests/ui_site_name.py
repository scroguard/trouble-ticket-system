"""Site name setting: API + server rendering + dashboard. Run on a freshly seeded stack."""
from common import BASE, COMPOSE, IMAP_PORT, REPO, SHOTS, SMTP_PORT, psql  # noqa: F401
import sys
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
    expect.set_options(timeout=15000)

    def ctx_page():
        page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
        page.on("console", lambda m: m.type in ("error", "warning") and "Failed to load resource" not in m.text
                and problems.append(f"console.{m.type}: {m.text}"))
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        return page

    # --- API: permissions and validation
    anon = ctx_page()
    anon.goto(B + "/")
    check(anon.title() == "Support Desk" and anon.locator("#login-site-name").inner_text() == "Support Desk",
          "default name rendered on sign-in page")
    check(anon.request.get(B + "/api/admin/settings").status == 401, "settings API requires sign-in")

    agent = ctx_page()
    agent.request.post(B + "/auth/login", data={"email": "alice@example.com", "password": "agent-pass-123"})
    check(agent.request.get(B + "/api/admin/settings").status == 403, "agent -> 403")
    check(agent.request.patch(B + "/api/admin/settings", data={"site_name": "Hacked"}, headers={"Origin": B}).status == 403,
          "agent can't change it")

    admin = ctx_page()
    admin.request.post(B + "/auth/login", data={"email": "ada@example.com", "password": "correct-horse-1"})
    for body, label in [({"site_name": "   "}, "blank"), ({"site_name": "x" * 101}, "too long"),
                        ({"site_name": "a\u0007b"}, "control char"), ({}, "missing"), ({"site_name": "ok", "x": 1}, "extra field")]:
        r = admin.request.patch(B + "/api/admin/settings", data=body, headers={"Origin": B})
        check(r.status == 400 and r.json()["error"] == "Validation failed", f"PATCH {label} -> 400")

    # --- server rendering escapes the name (it's admin input placed into HTML)
    evil = '</title><script>alert(1)</script> & "Co"'
    r = admin.request.patch(B + "/api/admin/settings", data={"site_name": evil}, headers={"Origin": B})
    check(r.status == 200 and r.json()["site_name"] == evil, "PATCH accepts punctuation")
    raw = admin.request.get(B + "/").text()
    check("<script>alert(1)</script>" not in raw and "&lt;/title&gt;&lt;script&gt;" in raw and "&quot;Co&quot;" in raw,
          "name is HTML-escaped in the served page")
    anon.goto(B + "/")
    check(anon.title() == evil and anon.locator("#brand-name").inner_text() == evil, "escaped name displays literally")

    # --- dashboard: admin renames the site
    admin.goto(B + "/")  # already signed in via the API calls above (shared cookie jar)
    expect(admin.locator("#app-view")).to_be_visible()
    admin.click("#admin-link")
    expect(admin.locator("#site-name-input")).to_have_value(evil)
    check(admin.title() == f"Users · {evil}", "admin view title uses site name")
    admin.fill("#site-name-input", "   ")
    admin.click("#site-save")
    expect(admin.locator("#site-error")).to_have_text("Enter a site name.")
    admin.fill("#site-name-input", "  Acme Helpdesk  ")
    admin.click("#site-save")
    expect(admin.locator(".toast", has_text="Site name updated")).to_be_visible()
    expect(admin.locator("#brand-name")).to_have_text("Acme Helpdesk")
    check(admin.locator("#site-name-input").input_value() == "Acme Helpdesk", "saved (trimmed) and header updated live")
    check(admin.title() == "Users · Acme Helpdesk", "tab title updated live")
    admin.screenshot(path=f"{SHOTS}/site-settings.png")
    admin.click("#admin-back")
    check(admin.title() == "Acme Helpdesk", "ticket view title uses new name")

    # --- everyone else sees it on next load, starting with the sign-in page
    fresh = ctx_page()
    fresh.goto(B + "/")
    check(fresh.title() == "Acme Helpdesk" and fresh.locator("#login-site-name").inner_text() == "Acme Helpdesk",
          "new name on sign-in page for other users")
    agent.goto(B + "/")
    expect(agent.locator("#brand-name")).to_have_text("Acme Helpdesk")
    check(not agent.locator("#admin-link").is_visible(), "agent sees new name, no admin access")

    # --- change own password (agent) still available from the user menu
    agent.click(".user-menu-btn")
    agent.click("#change-password-open")
    expect(agent.locator("#password-modal.show")).to_be_visible()
    agent.fill("#pw-current", "agent-pass-123")
    agent.fill("#pw-new", "new-agent-pass-456")
    agent.fill("#pw-confirm", "new-agent-pass-456")
    agent.locator("#password-form button[type=submit]").click()
    expect(agent.locator(".toast", has_text="Password changed")).to_be_visible()
    r = anon.request.post(B + "/auth/login", data={"email": "alice@example.com", "password": "new-agent-pass-456"})
    check(r.status == 200, "agent changed own password via user menu; new password works")

    check(not problems, "no console errors / CSP violations / page errors")
    for pr in problems:
        print("   ", pr)
    browser.close()

sys.exit(0 if ok else 1)
