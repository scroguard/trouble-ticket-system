# Automated tests

End-to-end suites that run against a real, throwaway copy of the system: PostgreSQL,
the web app, the worker, and the bundled GreenMail test mail server. They send real
email, click through the dashboard and portal in a real (headless) browser, and check
what customers and agents actually receive.

## Running them

Requirements: Docker with Compose v2, `python3` and the `requests` package
(`pip install requests`). The browser suites download the official Playwright image
on first use.

```bash
tests/run.sh                        # all suites (roughly 30 minutes)
tests/run.sh api reply_alerts loop  # only some
```

Output looks like:

```
api            ok    (54 checks)
ui_portal      ok    (21 checks)
...
All suites passed.
```

Each suite's full log is in `tests/.logs/<suite>.log`; screenshots from the browser
suites go to `tests/.shots/`. Both folders are ignored by Git.

## Safe to run anywhere

- Every suite runs under its own Docker Compose project, **`tickets-test`**, and
  starts from an empty database, which it deletes afterwards. The runner refuses any
  project name that doesn't end in `-test`, so it can't touch a real deployment,
  whether that's run with plain `docker compose` (project `tickets`) or managed by
  Dockhand/Portainer.
- It uses its own ports: **18000** (web), **13025** (SMTP) and **13143** (IMAP).
  Change them with `TT_WEB_PORT`, `TT_SMTP_PORT` and `TT_IMAP_PORT` if they're taken.
- All test settings come from `tests/compose.test.yml` and override your `.env`, so
  your real mail server is never contacted. If there's no `.env` yet, the runner copies
  `.env.example` (Compose needs the file to exist).

It's still best run on a development machine: it builds images and uses a few GB
of RAM while running.

## The suites

| Suite | What it covers |
|---|---|
| `api` | Sign-in, sessions, rate limits, ticket list/filters/search, assignment + notification email, comments, email delivery, cross-site protection |
| `api_admin` | User management, password changes, sign-in lockouts, reply retry after SMTP outages |
| `ingestion` | Email → tickets: threading, quoting, attachments, HTML cleaning, priority escalation, duplicates, retries |
| `portal_api` | Customer portal: sign-in links, isolation between customers, internal notes never exposed, attachments, limits |
| `reply_alerts` | Staff alerts on customer replies: who gets them, throttling, reopen wording, portal replies |
| `loop` | Mail-loop protection against auto-responders and bounces |
| `ui_dashboard` | Agent dashboard in a browser: list, filters, conversation, replies, mobile, dark mode |
| `ui_admin` | Admin section: add/edit/deactivate users, reset passwords, unlock |
| `ui_site_name` | Admin-editable site name |
| `ui_signature` | Agent signatures in the browser, in emails and in the portal |
| `ui_portal` | Customer portal in a browser: emailed sign-in link through to "This is solved" |

Files: one Python script per suite, `seed.py` (sample data), `common.py` (shared
settings), `compose.*.yml` (test configuration) and `run.sh` (the runner).
