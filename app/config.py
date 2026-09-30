"""Application settings, loaded from environment variables / .env."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Core ---------------------------------------------------------------
    app_name: str = "Trouble Ticket System"
    app_base_url: str = "http://localhost:8000"
    secret_key: str = Field(min_length=32)
    database_url: str  # e.g. postgresql+psycopg://user:pass@db:5432/tickets
    log_level: str = "INFO"

    # --- IMAP (inbound) -----------------------------------------------------
    imap_host: str
    imap_port: int = 993
    imap_use_ssl: bool = True           # implicit TLS (usually port 993)
    imap_use_starttls: bool = False     # upgrade plain connection (usually port 143)
    imap_user: str
    imap_password: str
    imap_mailbox: str = "INBOX"
    imap_poll_interval: int = 60        # seconds between polls
    imap_timeout: int = 30              # socket timeout, seconds
    imap_batch_size: int = 50           # max messages handled per poll
    imap_max_failures: int = 3          # attempts before a message is quarantined

    # --- SMTP (outbound) ----------------------------------------------------
    smtp_host: str
    smtp_port: int = 587
    smtp_use_ssl: bool = False          # implicit TLS (usually port 465)
    smtp_use_starttls: bool = True      # STARTTLS (usually port 587)
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from_address: str              # the support mailbox customers reply to
    smtp_from_name: str = "Support"
    smtp_timeout: int = 30

    # --- Retry policy (shared by IMAP & SMTP) -------------------------------
    email_max_retries: int = 4
    email_retry_base_delay: float = 2.0  # seconds; exponential backoff + jitter
    email_retry_max_delay: float = 60.0

    # --- Attachments --------------------------------------------------------
    attachment_dir: Path = Path("/data/attachments")
    attachment_max_bytes: int = 20 * 1024 * 1024

    # --- Priority ------------------------------------------------------------
    # Auto-escalate new email tickets that contain urgency keywords (URGENT, DOWN...).
    # Sender-set importance headers (X-Priority / Importance) are honored regardless.
    priority_escalation_enabled: bool = True

    # --- Auth ----------------------------------------------------------------
    session_cookie_name: str = "tts_session"
    session_ttl_hours: int = 12
    # None = derive from APP_BASE_URL (Secure cookies whenever it is https://).
    session_cookie_secure: bool | None = None
    # Login rate limits (sliding windows over the login_failures log):
    login_max_attempts: int = 5                # per account + IP ...
    login_lockout_minutes: int = 15            # ... and per IP, within this window
    login_max_attempts_per_ip: int = 20
    login_max_attempts_per_account: int = 50   # all IPs combined, per hour
    # Extra origins allowed to make state-changing browser requests (comma-separated).
    trusted_origins: str = ""

    # --- Staff alerts ------------------------------------------------------
    # Email the assignee (or all staff if unassigned) when a customer replies.
    customer_reply_alerts: bool = True
    # A burst of customer messages produces one alert; a new one is sent after this
    # long, or as soon as an agent has answered in between.
    customer_reply_alert_cooldown_minutes: int = 5

    # Loop breaker: at most this many "we received your request" acknowledgements per
    # address per hour, whatever the other side's auto-responder does.
    ack_max_per_address_per_hour: int = 3

    # --- Customer portal (/portal) ----------------------------------------
    portal_enabled: bool = True
    portal_link_minutes: int = 15             # how long an emailed sign-in link works
    portal_session_days: int = 14             # how long a customer stays signed in
    portal_links_per_hour: int = 5            # sign-in emails per address per hour
    portal_links_per_ip_per_hour: int = 20
    portal_tickets_per_hour: int = 10         # new tickets per customer per hour
    portal_replies_per_hour: int = 30
    portal_max_files: int = 5                 # attachments per message (each <= ATTACHMENT_MAX_BYTES)

    # --- Reply delivery retries --------------------------------------------
    reply_max_attempts: int = 6                # then give up (agents can resend)
    reply_retry_base_seconds: int = 300        # 5m, 10m, 20m, 40m, 80m ...
    reply_pending_timeout_seconds: int = 600   # sweep "pending" replies stuck this long

    # --- Worker -------------------------------------------------------------
    worker_heartbeat_file: Path = Path("/tmp/worker-heartbeat")

    @property
    def cookie_secure(self) -> bool:
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure
        return self.app_base_url.startswith("https://")

    @property
    def allowed_origins(self) -> set[str]:
        from urllib.parse import urlsplit

        base = urlsplit(self.app_base_url)
        extra = {o.strip().rstrip("/") for o in self.trusted_origins.split(",") if o.strip()}
        return {f"{base.scheme}://{base.netloc}"} | extra

    @model_validator(mode="after")
    def _check_tls_flags(self) -> "Settings":
        if self.imap_use_ssl and self.imap_use_starttls:
            raise ValueError("IMAP_USE_SSL and IMAP_USE_STARTTLS are mutually exclusive")
        if self.smtp_use_ssl and self.smtp_use_starttls:
            raise ValueError("SMTP_USE_SSL and SMTP_USE_STARTTLS are mutually exclusive")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
