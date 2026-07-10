"""Passwordless authentication, session, email, and rate-limit helpers."""

import hashlib
import re
import secrets
import smtplib
from collections import defaultdict, deque
from datetime import timedelta
from email.message import EmailMessage
from threading import Lock
from time import monotonic

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db_models import AuthSession, MagicLink, User, utcnow
from api.settings import AppSettings

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_email(email: str) -> str:
    email = email.strip().casefold()
    if len(email) > 320 or not EMAIL_RE.fullmatch(email):
        raise ValueError("Enter a valid email address")
    return email


class RateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        now = monotonic()
        with self._lock:
            events = self._events[key]
            while events and events[0] <= now - window_seconds:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            return True


rate_limiter = RateLimiter()


def issue_magic_link(db: Session, email: str, ip: str | None) -> tuple[MagicLink, str]:
    email = normalize_email(email)
    token = secrets.token_urlsafe(32)
    link = MagicLink(
        email=email,
        token_hash=hash_token(token),
        expires_at=utcnow() + timedelta(minutes=15),
        requested_ip=ip,
    )
    db.add(link)
    return link, token


def consume_magic_link(db: Session, token: str, settings: AppSettings) -> User | None:
    link = db.scalar(select(MagicLink).where(MagicLink.token_hash == hash_token(token)))
    if not link or link.used_at or link.expires_at <= utcnow():
        return None
    link.used_at = utcnow()
    user = db.scalar(select(User).where(User.email == link.email))
    if not user:
        user = User(email=link.email)
        db.add(user)
        db.flush()
    user.is_admin = user.email in settings.admin_emails
    return user


def create_auth_session(db: Session, user: User) -> tuple[AuthSession, str]:
    raw_token = secrets.token_urlsafe(32)
    session = AuthSession(
        user_id=user.id,
        token_hash=hash_token(raw_token),
        csrf_token=secrets.token_urlsafe(24),
        expires_at=utcnow() + timedelta(days=30),
    )
    db.add(session)
    return session, raw_token


def get_auth_session(db: Session, raw_token: str | None) -> AuthSession | None:
    if not raw_token:
        return None
    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == hash_token(raw_token)))
    if not session or session.expires_at <= utcnow():
        return None
    return session


def send_magic_link(settings: AppSettings, recipient: str, token: str) -> None:
    if not settings.smtp_host:
        if settings.magic_link_debug:
            return
        raise RuntimeError("Email delivery is not configured")
    url = f"{settings.public_base_url}/auth/verify?token={token}"
    message = EmailMessage()
    message["Subject"] = "Your Paperpulse sign-in link"
    message["From"] = settings.smtp_from
    message["To"] = recipient
    message.set_content(f"Sign in to Paperpulse using this link (valid for 15 minutes):\n\n{url}")
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password or "")
        smtp.send_message(message)
