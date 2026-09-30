"""Background worker: `python -m app.worker`.

Each tick it may:
  * poll IMAP for new mail (every IMAP_POLL_INTERVAL, backing off while it fails),
  * retry customer replies whose delivery is due (every tick),
  * prune expired sessions and old login failures (every HOUSEKEEPING_INTERVAL).
The three are independent: an IMAP outage doesn't delay reply retries and vice versa.
"""

import logging
import signal
import threading
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

from app.config import get_settings
from app.db import session_scope
from app.email_service import EmailService, PermanentEmailError
from app.ingestion import TicketIngestor
from app.models import CustomerLoginToken, CustomerSession, LoginFailure, UserSession
from app.notifications import retry_due_replies

log = logging.getLogger("app.worker")

MAX_BACKOFF_SECONDS = 900
MAX_TICK_SECONDS = 15
HOUSEKEEPING_INTERVAL = 600
LOGIN_FAILURE_RETENTION = timedelta(days=1)


def housekeeping() -> None:
    now = datetime.now(UTC)
    with session_scope() as db:
        sessions = db.execute(delete(UserSession).where(UserSession.expires_at <= now)).rowcount
        failures = db.execute(
            delete(LoginFailure).where(LoginFailure.attempted_at < now - LOGIN_FAILURE_RETENTION)
        ).rowcount
        # Portal: expired customer sessions, and sign-in links older than a day (they
        # double as the rate-limit log, which only looks back one hour).
        sessions += db.execute(delete(CustomerSession).where(CustomerSession.expires_at <= now)).rowcount
        db.execute(delete(CustomerLoginToken).where(CustomerLoginToken.created_at < now - LOGIN_FAILURE_RETENTION))
    if sessions or failures:
        log.info("Housekeeping: removed %d expired sessions, %d old login failures", sessions, failures)


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    email_service = EmailService(settings)
    ingestor = TicketIngestor(settings, email_service)

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    log.info(
        "Worker started: %s:%s (ssl=%s, starttls=%s) mailbox=%s every %ss",
        settings.imap_host, settings.imap_port, settings.imap_use_ssl,
        settings.imap_use_starttls, settings.imap_mailbox, settings.imap_poll_interval,
    )

    tick = min(settings.imap_poll_interval, MAX_TICK_SECONDS)
    next_poll = next_housekeeping = time.monotonic()
    imap_failures = 0

    while not stop.is_set():
        now = time.monotonic()

        if now >= next_poll:
            try:
                result = email_service.poll_inbox(ingestor)
                if result.fetched:
                    log.info("Poll done: %s", result)
                imap_failures = 0
                settings.worker_heartbeat_file.touch()  # read by the container healthcheck
            except PermanentEmailError as exc:
                imap_failures += 1
                log.error("IMAP configuration/auth problem (check .env): %s", exc)
            except Exception:
                imap_failures += 1
                log.exception("Poll cycle failed")
            # Back off exponentially while the mail server keeps failing.
            delay = settings.imap_poll_interval * 2 ** min(imap_failures, 10)
            next_poll = time.monotonic() + min(delay, max(MAX_BACKOFF_SECONDS, settings.imap_poll_interval))

        try:
            if sent := retry_due_replies():
                log.info("Retried and delivered %d reply(ies)", sent)
        except Exception:
            log.exception("Reply retry sweep failed")

        if now >= next_housekeeping:
            try:
                housekeeping()
            except Exception:
                log.exception("Housekeeping failed")
            next_housekeeping = now + HOUSEKEEPING_INTERVAL

        stop.wait(tick)

    log.info("Worker stopped")


if __name__ == "__main__":
    main()
