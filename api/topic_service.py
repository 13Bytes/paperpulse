"""Topic validation and moderation helpers shared by web and import flows."""

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.arxiv_taxonomy import validate_categories
from api.db_models import Subscription, Topic, TopicTerm, User, utcnow


def normalize(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def slugify(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value.casefold()).strip("-")
    return slug[:90] or "topic"


def split_values(raw: str) -> list[str]:
    return list(dict.fromkeys(part.strip() for part in re.split(r"[,\n]", raw) if part.strip()))


def create_topic(
    db: Session,
    *,
    name: str,
    description: str,
    categories: list[str],
    keywords: list[str],
    creator: User | None,
    status: str = "pending",
) -> Topic:
    name = " ".join(name.split())
    description = description.strip()
    if len(name) < 3 or len(name) > 100:
        raise ValueError("Topic name must contain 3–100 characters")
    if len(description) < 10:
        raise ValueError("Please provide a short topic description")
    categories = validate_categories(categories)
    keywords = list(dict.fromkeys(" ".join(value.split()) for value in keywords if value.strip()))
    if not categories or not keywords:
        raise ValueError("At least one category and one keyword are required")
    if len(categories) > 20 or len(keywords) > 30:
        raise ValueError("Too many initial search terms")
    normalized = normalize(name)
    if db.scalar(select(Topic).where(Topic.name_normalized == normalized)):
        raise ValueError("A topic with this name already exists or is pending")
    base_slug = slugify(name)
    slug = base_slug
    suffix = 2
    while db.scalar(select(Topic).where(Topic.slug == slug)):
        slug = f"{base_slug}-{suffix}"
        suffix += 1
    topic = Topic(
        slug=slug,
        name=name,
        name_normalized=normalized,
        description=description,
        status=status,
        created_by_id=creator.id if creator else None,
    )
    db.add(topic)
    db.flush()
    term_status = "active" if status == "active" else "pending"
    for kind, values in (("category", categories), ("keyword", keywords)):
        for value in values:
            db.add(
                TopicTerm(
                    topic_id=topic.id,
                    kind=kind,
                    value=value,
                    value_normalized=normalize(value),
                    status=term_status,
                    created_by_id=creator.id if creator else None,
                )
            )
    return topic


def propose_terms(
    db: Session,
    topic: Topic,
    user: User,
    categories: list[str],
    keywords: list[str],
    *,
    status: str = "pending",
) -> list[TopicTerm]:
    categories = validate_categories(categories)
    keywords = list(dict.fromkeys(" ".join(value.split()) for value in keywords if value.strip()))
    if not categories and not keywords:
        raise ValueError("Enter at least one category or keyword")
    created = []
    for kind, values in (("category", categories), ("keyword", keywords)):
        for value in values:
            value_normalized = normalize(value)
            exists = db.scalar(
                select(TopicTerm).where(
                    TopicTerm.topic_id == topic.id,
                    TopicTerm.kind == kind,
                    TopicTerm.value_normalized == value_normalized,
                )
            )
            if exists:
                continue
            term = TopicTerm(
                topic_id=topic.id,
                kind=kind,
                value=value,
                value_normalized=value_normalized,
                status=status,
                created_by_id=user.id,
            )
            db.add(term)
            created.append(term)
    if not created:
        raise ValueError("All suggested terms already exist or are pending")
    return created


def review_topic(db: Session, topic: Topic, admin: User, approved: bool, reason: str = "") -> None:
    now = utcnow()
    topic.status = "active" if approved else "rejected"
    topic.reviewed_by_id = admin.id
    topic.reviewed_at = now
    topic.review_reason = reason.strip() or None
    for term in topic.terms:
        if term.status == "pending":
            term.status = "active" if approved else "rejected"
            term.reviewed_by_id = admin.id
            term.reviewed_at = now
            term.review_reason = topic.review_reason
    if approved and topic.created_by_id:
        existing = db.get(Subscription, (topic.created_by_id, topic.id))
        if not existing:
            db.add(Subscription(user_id=topic.created_by_id, topic_id=topic.id))


def review_term(term: TopicTerm, admin: User, approved: bool, reason: str = "") -> None:
    term.status = "active" if approved else "rejected"
    term.reviewed_by_id = admin.id
    term.reviewed_at = utcnow()
    term.review_reason = reason.strip() or None


def active_term_values(topic: Topic, kind: str) -> list[str]:
    return [term.value for term in topic.terms if term.kind == kind and term.status == "active"]
