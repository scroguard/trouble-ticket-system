"""Admin section, driven in headless Chromium. Run on a freshly seeded stack."""
from common import BASE, COMPOSE, IMAP_PORT, REPO, SHOTS, SMTP_PORT, psql  # noqa: F401
import sys, time
from playwright.sync_api import sync_playwright, expect

B = BASE
ok = True
problems = []


def check(cond, label):
    global ok
    print(("PASS " if cond else "FAIL ") + label, flush=True)
    ok &= bool(cond)


def watch(page, tag):
    page.on("console", lambda m: m.type in ("error", "warning") and "Failed to load resource" not in m.text
            and problems.append(f"{tag} console.{m.type}: {m.text}"))
    page.on("pageerror", lambda e: problems.append(f"{tag} pageerror: {e}"))


def login(browser, email, password, viewport=(1440, 900)):
    ctx = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]})
    page = ctx.new_page()
    watch(page, email)
    page.goto(B + "/")
    page.fill("#login-email", email)
    page.fill("#login-password", password)
    page.click("#login-submit")
    return ctx, page


def row(page, name):
    return page.locator("#admin-rows tr", has_text=name)


with sync_playwright() as p:
    browser = p.chromium.launch()
    expect.set_options(timeout=15000)

    # --- agents don't get the admin section
    _, alice = login(browser, "alice@example.com", "agent-pass-123")
    expect(alice.locator("#app-view")).to_be_visible()
    check(not alice.locator("#admin-link").is_visible(), "agent: no Admin button")
    alice.goto(B + "/#/admin")
    expect(alice.locator(".toast", has_text="only available to admins")).to_be_visible()
    check(not alice.locator("#admin-view").is_visible() and alice.url.endswith("/"), "agent: #/admin redirected with notice")
    r = alice.request.get(B + "/api/admin/users")
    check(r.status == 403, "agent: /api/admin/users -> 403")

    # --- admin opens the section
    ctx, page = login(browser, "ada@example.com", "correct-horse-1")
    expect(page.locator("#admin-link")).to_be_visible()
    bob_id = page.request.get(B + "/api/users?role=agent").json()
    bob_id = next(u["id"] for u in bob_id if u["email"] == "bob@example.com")
    tickets = page.request.get(B + "/api/tickets?limit=10").json()["items"]
    for t in tickets[:2]:
        page.request.patch(B + f"/api/tickets/{t['id']}", data={"assigned_to": bob_id}, headers={"Origin": B})
    page.click("#admin-link")
    expect(page.locator("#admin-view")).to_be_visible()
    check(page.url.endswith("#/admin") and not page.locator(".panes").is_visible(), "admin view replaces ticket panes")
    expect(page.locator("#admin-rows tr")).to_have_count(3)
    expect(row(page, "Bob Agent").locator("td").nth(4)).to_have_text("2")
    check(True, "users table with open-ticket counts (Bob: 2)")
    check(row(page, "Ada Admin").locator(".badge", has_text="you").count() == 1, "own row marked 'you'")
    check(row(page, "Ada Admin").locator('[aria-label^="Deactivate"]').count() == 0, "no deactivate button on own row")
    expect(page.locator("#admin-summary .stat").first).to_contain_text("2")
    check("Active agents" in page.locator("#admin-summary").inner_text(), "summary stats")
    page.screenshot(path=f"{SHOTS}/a1-admin.png")

    # --- add user with generated password
    page.click("#admin-add")
    expect(page.locator("#user-modal")).to_be_visible()
    generated = page.locator("#u-password").input_value()
    check(len(generated) == 19 and generated.count("-") == 3, f"password pre-generated ({generated})")
    page.click("#user-submit")
    expect(page.locator("#user-error")).to_have_text("Enter a name.")
    page.fill("#u-name", "Gina Newhire")
    page.fill("#u-email", "alice@example.com")
    page.click("#user-submit")
    expect(page.locator("#user-error")).to_contain_text("already exists")
    check(True, "client + server validation shown in modal")
    page.fill("#u-email", "gina@example.com")
    page.click("#user-submit")
    expect(page.locator("#secret-modal")).to_be_visible()
    check(page.locator("#secret-value").input_value() == generated, "credentials dialog shows the password once")
    page.click("#secret-copy")
    expect(page.locator(".toast", has_text="Copied")).to_be_visible()
    page.locator("#secret-modal").get_by_role("button", name="Done").click()
    expect(page.locator("#secret-modal")).to_be_hidden()
    check(page.locator("#secret-value").input_value() == "", "password cleared from DOM after closing")
    expect(row(page, "Gina Newhire")).to_be_visible()
    _, gina = login(browser, "gina@example.com", generated)
    expect(gina.locator("#app-view")).to_be_visible()
    check(True, "new user can sign in with the shared password")

    # --- edit: promote Gina
    row(page, "Gina Newhire").locator('[aria-label^="Edit"]').click()
    expect(page.locator("#user-modal.show")).to_be_visible()
    expect(page.locator("#u-password-group")).to_be_hidden()
    page.select_option("#u-role", "admin")
    page.fill("#u-name", "Gina Lead")
    page.click("#user-submit")
    expect(row(page, "Gina Lead").locator(".role-admin:visible")).to_be_visible()
    check(True, "edit name + promote to admin")
    row(page, "Ada Admin").locator('[aria-label^="Edit"]').click()  # right after saving: dialog is still fading out
    expect(page.locator("#user-modal.show #u-role-self")).to_be_visible()
    check(page.locator("#u-role").is_disabled() and page.locator("#u-name").input_value() == "Ada Admin",
          "Edit right after Save still opens (own role locked)")
    expect(page.locator("#u-name")).to_be_focused()  # fully shown (Bootstrap ignores Esc mid-fade)
    page.keyboard.press("Escape")
    expect(page.locator("#user-modal")).to_be_hidden()

    # --- failed sign-ins -> badge -> clear
    for _ in range(3):
        page.request.post(B + "/auth/login", data={"email": "alice@example.com", "password": "wrong-wrong"})
    page.click("#admin-link") if page.locator("#admin-link").is_visible() else None
    page.evaluate("loadAdminUsers()")
    expect(row(page, "Alice Agent").locator(".badge", has_text="3 failed")).to_be_visible()
    row(page, "Alice Agent").locator('[aria-label^="Clear failed"]').click()
    expect(page.locator(".toast", has_text="Cleared 3 failed sign-in attempts")).to_be_visible()
    expect(row(page, "Alice Agent").locator(".badge", has_text="failed")).to_have_count(0)
    check(True, "failed sign-in badge + clear")

    # --- reset password
    row(page, "Gina Lead").locator('[aria-label^="Reset password"]').click()
    newpw = page.locator("#reset-password").input_value()
    check(newpw != generated, "reset dialog generates a fresh password")
    page.click("#reset-submit")
    expect(page.locator("#secret-modal")).to_be_visible()
    check(page.locator("#secret-value").input_value() == newpw, "reset shows new password")
    page.locator("#secret-modal").get_by_role("button", name="Done").click()
    gina.reload()
    expect(gina.locator("#login-view")).to_be_visible()
    check(True, "reset signed Gina out")
    _, gina2 = login(browser, "gina@example.com", newpw)
    expect(gina2.locator("#app-view")).to_be_visible()
    check(True, "Gina signs in with the reset password")

    # --- deactivate Bob, unassigning his tickets
    _, bob = login(browser, "bob@example.com", "agent-pass-123")
    expect(bob.locator("#app-view")).to_be_visible()
    row(page, "Bob Agent").locator('[aria-label^="Deactivate"]').click()
    expect(page.locator("#confirm-modal")).to_be_visible()
    check(page.locator("#confirm-unassign").is_checked(), "deactivate offers to unassign 2 open tickets")
    page.screenshot(path=f"{SHOTS}/a2-deactivate.png")
    page.click("#confirm-ok")
    expect(page.locator(".toast", has_text="Bob Agent deactivated; 2 tickets unassigned")).to_be_visible()
    expect(row(page, "Bob Agent").locator(".badge", has_text="Inactive")).to_be_visible()
    expect(row(page, "Bob Agent").locator("td").nth(4)).to_have_text("0")
    check(True, "Bob deactivated, tickets unassigned")
    left = page.request.get(B + f"/api/tickets?assigned_to={bob_id}").json()["total"]
    check(left == 0, "no tickets left assigned to Bob")
    bob.evaluate("refreshList()")
    expect(bob.locator("#login-view")).to_be_visible()
    check(True, "Bob's open session ends on next request")
    page.uncheck("#admin-show-inactive")
    expect(row(page, "Bob Agent")).to_have_count(0)
    page.check("#admin-show-inactive")
    page.fill("#admin-search", "gina")
    expect(page.locator("#admin-rows tr")).to_have_count(1)
    page.fill("#admin-search", "")
    check(True, "show-inactive switch + search filter")

    # --- cancel keeps things unchanged; reactivate works
    row(page, "Alice Agent").locator('[aria-label^="Deactivate"]').click()
    page.locator("#confirm-modal").get_by_role("button", name="Cancel").click()
    expect(page.locator("#confirm-modal")).to_be_hidden()
    check(row(page, "Alice Agent").locator(".badge", has_text="Active").count() == 1, "cancel leaves user active")
    row(page, "Bob Agent").locator('[aria-label^="Reactivate"]').click()
    expect(row(page, "Bob Agent").locator(".badge", has_text="Active")).to_be_visible()
    check(True, "reactivate")

    # --- assignee dropdown reflects directory changes; back to tickets keeps selection
    page.click("#admin-back")
    expect(page.locator(".panes")).to_be_visible()
    page.locator(".ticket-card").first.click()
    expect(page.locator("#t-assignee option", has_text="Gina Lead")).to_have_count(1)
    check(True, "back to tickets; assignee list includes new user")
    page.click(".user-menu-btn")
    page.locator(".dropdown-menu .admin-only a").click()
    expect(page.locator("#admin-view")).to_be_visible()
    page.click("#admin-back")
    expect(page.locator("#workspace-body")).to_be_visible()
    check(True, "menu 'Manage users' works; back returns to the open ticket")

    # --- demoted while in the admin view -> graceful exit
    page.click("#admin-link")
    gina2.request.patch(B + f"/api/users/{page.evaluate('state.me.id')}", data={"role": "agent"}, headers={"Origin": B})
    page.evaluate("loadAdminUsers()")
    expect(page.locator(".toast", has_text="no longer have admin access")).to_be_visible()
    expect(page.locator(".panes")).to_be_visible()
    check(not page.locator("#admin-link").is_visible(), "demoted admin is moved out and loses the Admin button")

    # --- mobile
    _, m = login(browser, "gina@example.com", newpw, viewport=(390, 844))
    m.goto(B + "/#/admin")
    expect(m.locator("#admin-rows tr")).to_have_count(4)
    check(m.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "mobile admin: no page-level horizontal scroll")
    check(m.evaluate("(() => { const t = document.querySelector('.admin-view .table-responsive'); return t.scrollWidth <= t.clientWidth; })()"),
          "mobile admin: table fits without sideways scrolling")
    m.screenshot(path=f"{SHOTS}/a3-admin-mobile.png", full_page=True)

    check(not problems, "no console errors / CSP violations / page errors")
    for pr in problems:
        print("   ", pr)
    browser.close()

sys.exit(0 if ok else 1)
