from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from api.auth import consume_magic_link, create_auth_session, get_auth_session, issue_magic_link
from api.database import create_db_engine, create_schema, get_db
from api.db_models import Report, Subscription, User, utcnow
from api.importer import import_existing
from api.pipeline import run_daily, run_weekly
from api.settings import AppSettings
from api.topic_service import create_topic, propose_terms, review_term, review_topic
from api.webapp import app


@pytest.fixture
def db_factory():
    engine = create_db_engine("sqlite:///:memory:")
    create_schema(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def sample_paper():
    return {
        "title": "Test Paper Title",
        "authors": ["Author One", "Author Two"],
        "summary": "A test abstract.",
        "url": "https://arxiv.org/abs/2607.00001",
    }


def add_active_topic(db, name="AI Engineering", creator=None):
    return create_topic(
        db,
        name=name,
        description="Research on artificial intelligence applied to engineering.",
        categories=["cs.AI", "cs.RO"],
        keywords=["engineering automation", "generative design"],
        creator=creator,
        status="active",
    )


def test_topic_review_and_term_review_subscribe_creator(db_factory):
    with db_factory() as db:
        user = User(email="reader@example.com")
        admin = User(email="admin@example.com", is_admin=True)
        db.add_all([user, admin])
        db.flush()
        topic = create_topic(
            db,
            name="3D Printing",
            description="Additive manufacturing research and new printing methods.",
            categories=["cs.CE"],
            keywords=["additive manufacturing"],
            creator=user,
        )
        review_topic(db, topic, admin, True)
        db.flush()
        assert topic.status == "active"
        assert {term.status for term in topic.terms} == {"active"}
        assert db.get(Subscription, (user.id, topic.id)) is not None

        term = propose_terms(db, topic, user, ["cs.RO"], ["3D printing"])[0]
        review_term(term, admin, False, "Too broad")
        assert term.status == "rejected"
        assert term.review_reason == "Too broad"


def test_magic_link_is_hashed_single_use_and_bootstraps_admin(db_factory):
    settings = AppSettings(
        project_env="test",
        project_dir=Path.cwd(),
        openai_model="unused",
        admin_emails=("admin@example.com",),
    )
    with db_factory() as db:
        link, token = issue_magic_link(db, "Admin@Example.com", "127.0.0.1")
        db.flush()
        assert token not in link.token_hash
        user = consume_magic_link(db, token, settings)
        assert user and user.is_admin
        assert consume_magic_link(db, token, settings) is None
        db.flush()
        auth, raw = create_auth_session(db, user)
        db.flush()
        assert get_auth_session(db, raw).id == auth.id
        auth.expires_at = utcnow() - timedelta(seconds=1)
        assert get_auth_session(db, raw) is None


def test_daily_pipeline_is_per_topic_idempotent_and_skips_archived(
    monkeypatch, db_factory, sample_paper
):
    with db_factory() as db:
        active = add_active_topic(db)
        archived = add_active_topic(db, "Archived topic")
        archived.status = "archived"
        db.commit()

    class FakeArxiv:
        queries = []

        def __init__(self, query, *_args):
            self.query = query
            self.queries.append(query)

        def retrieve_daily_results(self, now=None):
            return [sample_paper]

    class FakeBackend:
        def identify_important_papers(self, papers):
            return "## Theme 1\nTest Paper Title"

        def summarize_weekly(self, reports):
            return "## Weekly"

    monkeypatch.setattr("api.pipeline.ArxivClient", FakeArxiv)
    monkeypatch.setattr("api.pipeline.create_summary_backend", lambda *_args: FakeBackend())
    monkeypatch.setattr("api.pipeline.load_config", lambda: {"summarization": {}})
    monkeypatch.setattr(
        "api.pipeline.load_app_settings",
        lambda: AppSettings(project_env="test", project_dir=Path.cwd(), openai_model="unused"),
    )
    now = datetime(2026, 7, 10, 6, tzinfo=UTC)
    first = run_daily(now=now, session_factory=db_factory)
    second = run_daily(now=now, session_factory=db_factory)

    assert first.succeeded == 1 and first.failed == 0
    assert second.skipped == 1
    assert len(FakeArxiv.queries) == 1
    assert "cat:cs.AI+OR+cat:cs.RO" in FakeArxiv.queries[0]
    assert "+AND+" in FakeArxiv.queries[0]
    with db_factory() as db:
        reports = list(db.scalars(select(Report)))
        assert len(reports) == 1 and reports[0].topic_id == active.id
        assert reports[0].period_start == date(2026, 7, 9)


def test_weekly_pipeline_uses_previous_monday_to_sunday(monkeypatch, db_factory):
    with db_factory() as db:
        topic = add_active_topic(db)
        for day in (date(2026, 6, 29), date(2026, 7, 2), date(2026, 7, 5)):
            db.add(
                Report(
                    topic_id=topic.id,
                    kind="daily",
                    title=str(day),
                    period_start=day,
                    period_end=day,
                    content_markdown=f"Daily {day}",
                    num_papers=2,
                )
            )
        db.commit()

    captured = []

    class FakeBackend:
        def summarize_weekly(self, reports):
            captured.extend(reports)
            return "## Weekly synthesis"

    monkeypatch.setattr("api.pipeline.create_summary_backend", lambda *_args: FakeBackend())
    monkeypatch.setattr("api.pipeline.load_config", lambda: {"summarization": {}})
    monkeypatch.setattr(
        "api.pipeline.load_app_settings",
        lambda: AppSettings(project_env="test", project_dir=Path.cwd(), openai_model="unused"),
    )
    result = run_weekly(as_of=date(2026, 7, 6), session_factory=db_factory)
    assert result.succeeded == 1
    assert len(captured) == 3
    with db_factory() as db:
        weekly = db.scalar(select(Report).where(Report.kind == "weekly"))
        assert (weekly.period_start, weekly.period_end, weekly.num_papers) == (
            date(2026, 6, 29),
            date(2026, 7, 5),
            6,
        )
        assert len(weekly.source_reports) == 3


def test_anonymous_selection_filters_home_feed(db_factory):
    with db_factory() as db:
        first = add_active_topic(db)
        second = add_active_topic(db, "AI General")
        db.flush()
        db.add_all(
            [
                Report(
                    topic_id=first.id,
                    kind="daily",
                    title="Engineering report",
                    period_start=date.today(),
                    period_end=date.today(),
                    content_markdown="One",
                ),
                Report(
                    topic_id=second.id,
                    kind="daily",
                    title="General report",
                    period_start=date.today(),
                    period_end=date.today(),
                    content_markdown="Two",
                ),
            ]
        )
        db.commit()
        first_id = first.id

    def override_db():
        with db_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        initial = client.get("/")
        assert initial.status_code == 200
        assert "Whatever your field, start with what interests you" in initial.text
        csrf = client.cookies["paperpulse_csrf"]
        response = client.post(
            "/subscriptions",
            data={"csrf_token": csrf, "topic_ids": str(first_id)},
            follow_redirects=True,
        )
        assert "Engineering report" in response.text
        assert "General report" not in response.text
    finally:
        app.dependency_overrides.clear()


def test_import_existing_is_idempotent(monkeypatch, db_factory, tmp_path):
    posts = tmp_path / "blog" / "_posts"
    posts.mkdir(parents=True)
    (posts / "2025-01-04-daily-summary.markdown").write_text(
        "---\ntitle: Legacy report\ndate: 2025-01-04\nnum_papers: 7\n---\n\n## Imported content",
        encoding="utf-8",
    )
    config = {
        "blog": {"title": "Engineering Pulse", "description": "A sufficiently long description."},
        "search": {"categories": ["cs.AI"], "keywords": ["engineering"]},
    }
    monkeypatch.setattr("api.importer.load_config", lambda: config)
    monkeypatch.setattr(
        "api.importer.load_app_settings",
        lambda: AppSettings(project_env="test", project_dir=tmp_path, openai_model="unused"),
    )
    assert import_existing(session_factory=db_factory) == (1, 1)
    assert import_existing(session_factory=db_factory) == (0, 0)
