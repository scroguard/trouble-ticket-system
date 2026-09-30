"""User admin, reply retries and rate limiting. Run after api_test.py on the same stack."""
from common import BASE, COMPOSE, IMAP_PORT, REPO, SHOTS, SMTP_PORT, psql  # noqa: F401
import os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from api_basic import Client, check, inbox, wait_for, BASE  # noqa: E402  (api_test runs on import)
import api_basic as api_test

PROJ = REPO


def compose(*args):
    return subprocess.run([*COMPOSE, *args],
                          cwd=PROJ, capture_output=True, text=True).stdout


def psql(sql):
    return compose("exec", "-T", "db", "psql", "-U", "tickets", "-d", "tickets", "-tAc", sql).strip()


print("\n=== user administration")
ada, alice, alice2, bob, bob2, anon = (Client() for _ in range(6))
s, _, _ = ada.req("POST", "/auth/login", {"email": "ada@example.com", "password": "correct-horse-1"})
check(s == 200, "admin logs back in")
s, users, _ = ada.req("GET", "/api/users")
ids = {u["email"]: u["id"] for u in users}
ADA, ALICE, BOB = ids["ada@example.com"], ids["alice@example.com"], ids["bob@example.com"]
alice.req("POST", "/auth/login", {"email": "alice@example.com", "password": "agent-pass-123"})

s, b, _ = alice.req("PATCH", f"/api/users/{BOB}", {"full_name": "Hacked"})
check(s == 403 and b == {"error": "Admin role required"}, "agent cannot edit users -> 403")
s, b, _ = alice.req("POST", f"/api/users/{BOB}/password", {"password": "whatever-123"})
check(s == 403, "agent cannot reset passwords -> 403")
s, b, _ = alice.req("GET", f"/api/users/{BOB}")
check(s == 200 and b["email"] == "bob@example.com", "agent can view a user")

s, b, _ = ada.req("PATCH", f"/api/users/{ADA}", {"role": "agent"})
check(s == 409 and "last active admin" in b["error"], "can't demote last admin")
s, b, _ = ada.req("PATCH", f"/api/users/{ADA}", {"is_active": False})
check(s == 409, "can't deactivate last admin")
s, b, _ = ada.req("PATCH", f"/api/users/{ALICE}", {"role": "admin", "full_name": "Alice Admin"})
check(s == 200 and b["role"] == "admin" and b["full_name"] == "Alice Admin", "promote + rename")
s, b, _ = alice.req("PATCH", f"/api/users/{ADA}", {"role": "agent"})
check(s == 200 and b["role"] == "agent", "with 2 admins, one can be demoted (role checked live, no re-login)")
s, b, _ = alice.req("PATCH", f"/api/users/{ADA}", {"role": "admin"})
s, b, _ = ada.req("PATCH", f"/api/users/{ALICE}", {"role": "agent", "full_name": "Alice Agent"})
check(s == 200 and b["role"] == "agent", "restore roles")

for body, code, label in [({}, 400, "empty"), ({"email": "bob@example.com"}, 409, "duplicate email"),
                          ({"role": "boss"}, 400, "bad role"), ({"is_active": None}, 400, "null field")]:
    s, b, _ = ada.req("PATCH", f"/api/users/{ALICE}", body)
    check(s == code, f"PATCH user {label} -> {code}")
s, b, _ = ada.req("PATCH", "/api/users/99999", {"full_name": "x"})
check(s == 404 and b == {"error": "User not found"}, "unknown user -> 404")

s, b, _ = ada.req("PATCH", f"/api/users/{ALICE}", {"is_active": False})
check(s == 200 and not b["is_active"], "deactivate alice")
s, _, _ = alice.req("GET", "/auth/me")
check(s == 401, "deactivated user's session is dead immediately")
s, b, _ = anon.req("POST", "/auth/login", {"email": "alice@example.com", "password": "agent-pass-123"})
check(s == 401, "deactivated user can't log in")
s, users, _ = ada.req("GET", "/api/users")
check("alice@example.com" not in {u["email"] for u in users}, "inactive hidden from default list")
s, users, _ = ada.req("GET", "/api/users?include_inactive=true")
check("alice@example.com" in {u["email"] for u in users}, "include_inactive shows them")
ada.req("PATCH", f"/api/users/{ALICE}", {"is_active": True})
s, _, _ = alice.req("POST", "/auth/login", {"email": "alice@example.com", "password": "agent-pass-123"})
check(s == 200, "reactivated user logs in")

print("\n=== passwords")
s, b, _ = bob.req("POST", "/auth/login", {"email": "bob@example.com", "password": "agent-pass-123"})
check(s == 429, "bob still rate-limited from api_test")
s, _, _ = ada.req("POST", f"/api/users/{BOB}/password", {"password": "reset-pass-456"})
check(s == 204, "admin resets bob's password")
s, _, _ = bob.req("POST", "/auth/login", {"email": "bob@example.com", "password": "reset-pass-456"})
check(s == 200, "reset also clears bob's login failures")
bob2.req("POST", "/auth/login", {"email": "bob@example.com", "password": "reset-pass-456"})
s, b, _ = bob.req("POST", "/auth/change-password", {"current_password": "nope-nope-nope", "new_password": "brand-new-789"})
check(s == 400 and b["details"][0]["field"] == "current_password", "wrong current password -> 400")
s, b, _ = bob.req("POST", "/auth/change-password", {"current_password": "reset-pass-456", "new_password": "reset-pass-456"})
check(s == 400, "new must differ from current")
s, _, _ = bob.req("POST", "/auth/change-password", {"current_password": "reset-pass-456", "new_password": "brand-new-789"})
check(s == 204, "change own password")
check(bob.req("GET", "/auth/me")[0] == 200 and bob2.req("GET", "/auth/me")[0] == 401, "current session kept, other sessions revoked")
check(anon.req("POST", "/auth/login", {"email": "bob@example.com", "password": "brand-new-789"})[0] == 200, "new password works")
s, _, _ = ada.req("POST", f"/api/users/{BOB}/password", {"password": "reset-pass-000"})
check(bob.req("GET", "/auth/me")[0] == 401, "admin reset logs the user out everywhere")
bob.req("POST", "/auth/login", {"email": "bob@example.com", "password": "reset-pass-000"})

print("\n=== rate limiting")
fails = [anon.req("POST", "/auth/login", {"email": "alice@example.com", "password": "wrong-pass-x"})[0] for _ in range(5)]
s, b, h = anon.req("POST", "/auth/login", {"email": "alice@example.com", "password": "agent-pass-123"})
check(fails == [401] * 5 and s == 429 and int(h["Retry-After"]) > 0, "5 failures lock account from this IP (Retry-After set)")
inside = compose("exec", "-T", "web", "python", "-c",
    "import urllib.request,json; r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/auth/login',"
    "data=json.dumps({'email':'alice@example.com','password':'agent-pass-123'}).encode(),headers={'Content-Type':'application/json'})); print(r.status)")
check(inside.strip() == "200", "same account from a different IP still logs in (no lockout DoS)")
s, b, _ = ada.req("POST", f"/api/users/{ALICE}/unlock")
check(s == 200 and b["cleared_failures"] >= 5, "admin unlock clears failures")
check(anon.req("POST", "/auth/login", {"email": "alice@example.com", "password": "agent-pass-123"})[0] == 200, "alice unlocked")

print("\n=== reply delivery retries")
alice.req("POST", "/auth/login", {"email": "alice@example.com", "password": "agent-pass-123"})
s, page, _ = alice.req("GET", "/api/tickets?q=printer")
tid = page["items"][0]["id"]


def comment(cid):
    return next(c for c in alice.req("GET", f"/api/tickets/{tid}")[1]["comments"] if c["id"] == cid)


compose("stop", "mail")
s, c1, _ = alice.req("POST", f"/api/tickets/{tid}/comments", {"body": "Retry test one"})
r1 = c1["id"]
c = wait_for(lambda: (lambda c: c if c["delivery_status"] == "failed" else None)(comment(r1)), 60)
check(c and c["delivery_attempts"] == 1 and c["next_attempt_at"] and c["delivery_error"], "SMTP down: reply failed, retry scheduled")
compose("start", "mail")
c = wait_for(lambda: (lambda c: c if c["delivery_status"] == "sent" else None)(comment(r1)), 60)
check(c and c["delivery_attempts"] >= 2 and c["next_attempt_at"] is None and c["delivery_error"] is None,
      "worker retried and delivered once SMTP recovered")

compose("stop", "mail")
s, c2, _ = alice.req("POST", f"/api/tickets/{tid}/comments", {"body": "Retry test two"})
r2 = c2["id"]
c = wait_for(lambda: (lambda c: c if c["delivery_status"] == "failed" and c["next_attempt_at"] is None else None)(comment(r2)), 90)
check(c and c["delivery_attempts"] == 3, "gives up after REPLY_MAX_ATTEMPTS (next_attempt_at cleared)")
mid_after_fail = psql(f"SELECT message_id FROM ticket_comments WHERE id={r2}")
compose("start", "mail")
time.sleep(12)
check(comment(r2)["delivery_status"] == "failed", "given-up reply is not retried automatically")

s, b, _ = alice.req("POST", f"/api/tickets/{tid}/comments/{r2}/resend")
check(s == 202 and b["delivery_status"] == "pending" and b["delivery_attempts"] == 0, "manual resend accepted (202)")
c = wait_for(lambda: (lambda c: c if c["delivery_status"] == "sent" else None)(comment(r2)), 60)
check(c is not None, "resent reply delivered")
got = wait_for(lambda: [m for m in inbox("carol@client.test") if "Retry test two" in m.get_body(("plain",)).get_content()])
check(got and len(got) == 1 and got[0]["Message-ID"] == mid_after_fail, "customer got it once, with the Message-ID from the first attempt")
s, b, _ = alice.req("POST", f"/api/tickets/{tid}/comments/{r2}/resend")
check(s == 409 and b["error"] == "Reply was already delivered", "resend of delivered reply -> 409")
s, note, _ = alice.req("POST", f"/api/tickets/{tid}/comments", {"body": "note", "is_internal": True})
s, b, _ = alice.req("POST", f"/api/tickets/{tid}/comments/{note['id']}/resend")
check(s == 400, "resend internal note -> 400")
s, b, _ = alice.req("POST", f"/api/tickets/999999/comments/{r2}/resend")
check(s == 404 and b == {"error": "Comment not found"}, "resend on wrong ticket -> 404")

print("\n=== per-IP limit (last: it blocks this IP)")
codes = [anon.req("POST", "/auth/login", {"email": f"spray{i}@example.com", "password": "x"})[0] for i in range(25)]
s, b, h = anon.req("POST", "/auth/login", {"email": "ada@example.com", "password": "correct-horse-1"})
check(429 in codes and s == 429 and "Retry-After" in h, "spraying many accounts from one IP -> 429 for everyone from that IP")

sys.exit(0 if api_test.ok else 1)
