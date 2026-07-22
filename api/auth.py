"""Passwordless authentication, session, email, and rate-limit helpers."""

import hashlib
import re
import secrets
import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from api.db_models import AuthSession, MagicLink, Topic, TopicTerm, User, utcnow
from api.settings import AppSettings

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_email(email: str) -> str:
    email = email.strip().casefold()
    if len(email) > 320 or not EMAIL_RE.fullmatch(email):
        raise ValueError("Enter a valid email address")
    return email


def allow_login_request(db: Session, email: str, ip: str | None) -> bool:
    """Apply durable per-address and per-IP limits using issued magic-link records."""
    email = normalize_email(email)
    cutoff = utcnow() - timedelta(minutes=15)
    conditions = [MagicLink.email == email]
    if ip:
        conditions.append(MagicLink.requested_ip == ip)
    count = db.scalar(
        select(func.count(MagicLink.id)).where(
            MagicLink.created_at >= cutoff,
            or_(*conditions),
        )
    )
    return int(count or 0) < 5


def allow_proposal(db: Session, user_id: int) -> bool:
    """Limit persisted topic and term proposals per user over a rolling day."""
    cutoff = utcnow() - timedelta(days=1)
    topics = db.scalar(
        select(func.count(Topic.id)).where(
            Topic.created_by_id == user_id, Topic.created_at >= cutoff
        )
    )
    terms = db.scalar(
        select(func.count(TopicTerm.id)).where(
            TopicTerm.created_by_id == user_id, TopicTerm.created_at >= cutoff
        )
    )
    return int(topics or 0) + int(terms or 0) < 10


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
    link = db.scalar(
        select(MagicLink)
        .where(MagicLink.token_hash == hash_token(token))
        .with_for_update()
    )
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


def cleanup_expired_auth(db: Session) -> tuple[int, int]:
    """Delete expired one-time links and sessions, returning deleted row counts."""
    now = utcnow()
    links = db.execute(delete(MagicLink).where(MagicLink.expires_at <= now)).rowcount or 0
    sessions = db.execute(delete(AuthSession).where(AuthSession.expires_at <= now)).rowcount or 0
    return links, sessions


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
    tls_context = ssl.create_default_context()
    try:
        if settings.smtp_port == 465:
            smtp_client = smtplib.SMTP_SSL(
                settings.smtp_host,
                settings.smtp_port,
                timeout=20,
                context=tls_context,
            )
        else:
            smtp_client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
        with smtp_client as smtp:
            if settings.smtp_starttls and settings.smtp_port != 465:
                smtp.starttls(context=tls_context)
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password or "")
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise RuntimeError("We couldn't send the sign-in email. Please try again shortly.") from exc
