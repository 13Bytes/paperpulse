"""HTTP-level coverage for the multi-user authentication and moderation flows."""

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeSerializer
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from api.auth import create_auth_session
from api.database import create_db_engine, create_schema, get_db
from api.db_models import Report, Subscription, Topic, TopicTerm, User
from api.settings import AppSettings
from api.topic_service import create_topic, propose_terms
from api.webapp import app

ADMIN_EMAIL = "moderator@example.com"


@pytest.fixture
def db_factory():
    engine = create_db_engine("sqlite:///:memory:")
    create_schema(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def http_client(monkeypatch, db_factory):
    """Serve the real FastAPI application against an isolated database."""
    import api.webapp as webapp

    test_settings = AppSettings(
        project_env="test",
        project_dir=Path.cwd(),
        openai_model="unused",
        session_secret="http-integration-test-secret",
        admin_emails=(ADMIN_EMAIL,),
        magic_link_debug=True,
    )
    monkeypatch.setattr(webapp, "settings", test_settings)
    monkeypatch.setattr(
        webapp,
        "selection_signer",
        URLSafeSerializer(test_settings.session_secret, salt="paperpulse-selections"),
    )

    def override_db():
        with db_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()
        app.dependency_overrides.clear()


def _csrf(client: TestClient) -> str:
    """Ensure the client owns a CSRF cookie and return it."""
    client.get("/")
    return client.cookies["paperpulse_csrf"]


def _create_user_session(db_factory, email: str, *, is_admin: bool = False) -> str:
    with db_factory() as db:
        user = User(email=email, is_admin=is_admin)
        db.add(user)
        db.flush()
        _auth, raw_token = create_auth_session(db, user)
        db.commit()
    return raw_token


def _login_with_magic_link(
    client: TestClient, monkeypatch, email: str = "reader@example.com"
) -> str:
    """Request, confirm, and consume a magic link exclusively through HTTP."""
    captured = {}

    def capture_link(_settings, recipient: str, token: str) -> None:
        captured.update(recipient=recipient, token=token)

    monkeypatch.setattr("api.webapp.send_magic_link", capture_link)
    response = client.post(
        "/login",
        data={"csrf_token": _csrf(client), "email": email},
    )
    assert response.status_code == 200
    assert captured["recipient"] == email.casefold()

    token = captured["token"]
    confirmation = client.get("/auth/verify", params={"token": token})
    assert confirmation.status_code == 200
    assert "paperpulse_session" not in client.cookies
    verified = client.post(
        "/auth/verify",
        data={"csrf_token": client.cookies["paperpulse_csrf"], "token": token},
        follow_redirects=False,
    )
    assert verified.status_code == 303
    assert client.cookies.get("paperpulse_session")
    return token


def _active_topic(db, name: str, creator=None) -> Topic:
    return create_topic(
        db,
        name=name,
        description=f"A sufficiently detailed description for {name}.",
        categories=["cs.AI"],
        keywords=[f"{name} research"],
        creator=creator,
        status="active",
    )


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("/subscriptions", {"topic_ids": "1"}),
        ("/login", {"email": "reader@example.com"}),
        (
            "/topics/propose",
            {
                "name": "Test proposal",
                "description": "A sufficiently detailed description.",
                "categories": "cs.AI",
                "keywords": "testing",
            },
        ),
        ("/topics/example/suggest", {"categories": "cs.RO", "keywords": "robotics"}),
        ("/logout", {}),
        ("/account/delete", {"confirmation": "DELETE"}),
        (
            "/admin/topics",
            {
                "name": "Admin topic",
                "description": "A sufficiently detailed description.",
                "categories": "cs.AI",
                "keywords": "administration",
            },
        ),
        ("/admin/topics/1/review", {"action": "approve"}),
        ("/admin/terms/1/review", {"action": "approve"}),
        ("/admin/topics/1/edit", {"description": "Updated detailed description."}),
        ("/admin/topics/1/archive", {}),
    ],
)
def test_every_mutating_route_rejects_bad_csrf(http_client, path, data):
    response = http_client.post(path, data={"csrf_token": "not-the-cookie", **data})
    # A route may not exist in an older schema, but once present it must enforce CSRF.
    if response.status_code != 404:
        assert response.status_code == 403, response.text


@pytest.mark.parametrize(
    ("method", "path", "data"),
    [
        ("get", "/admin", None),
        (
            "post",
            "/admin/topics",
            {
                "name": "Admin topic",
                "description": "A sufficiently detailed description.",
                "categories": "cs.AI",
                "keywords": "administration",
            },
        ),
        ("post", "/admin/topics/1/review", {"action": "approve"}),
        ("post", "/admin/terms/1/review", {"action": "approve"}),
        ("post", "/admin/topics/1/edit", {"description": "Updated description"}),
        ("post", "/admin/topics/1/archive", {}),
    ],
)
def test_admin_routes_reject_unauthenticated_users(http_client, method, path, data):
    if method == "post":
        response = http_client.post(
            path, data={"csrf_token": _csrf(http_client), **(data or {})}
        )
    else:
        response = http_client.get(path)
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "data"),
    [
        ("get", "/admin", None),
        (
            "post",
            "/admin/topics",
            {
                "name": "Admin topic",
                "description": "A sufficiently detailed description.",
                "categories": "cs.AI",
                "keywords": "administration",
            },
        ),
        ("post", "/admin/topics/1/review", {"action": "approve"}),
        ("post", "/admin/terms/1/review", {"action": "approve"}),
        ("post", "/admin/topics/1/edit", {"description": "Updated description"}),
        ("post", "/admin/topics/1/archive", {}),
    ],
)
def test_admin_routes_reject_signed_in_non_admins(
    http_client, db_factory, method, path, data
):
    http_client.cookies.set(
        "paperpulse_session", _create_user_session(db_factory, "ordinary@example.com")
    )
    if method == "post":
        response = http_client.post(
            path, data={"csrf_token": _csrf(http_client), **(data or {})}
        )
    else:
        response = http_client.get(path)
    assert response.status_code == 403


def test_admin_allowlist_revokes_a_stale_persisted_admin(http_client, db_factory):
    http_client.cookies.set(
        "paperpulse_session",
        _create_user_session(db_factory, "removed-admin@example.com", is_admin=True),
    )

    assert http_client.get("/admin").status_code == 403


def test_magic_link_login_session_and_logout(http_client, monkeypatch, db_factory):
    token = _login_with_magic_link(http_client, monkeypatch, "New.Reader@Example.com")

    account = http_client.get("/account")
    assert account.status_code == 200
    assert "new.reader@example.com" in account.text

    reused = http_client.post(
        "/auth/verify",
        data={"csrf_token": http_client.cookies["paperpulse_csrf"], "token": token},
    )
    invalid = http_client.post(
        "/auth/verify",
        data={"csrf_token": http_client.cookies["paperpulse_csrf"], "token": "invalid-token"},
    )
    assert reused.status_code == invalid.status_code == 400

    logout = http_client.post(
        "/logout",
        data={"csrf_token": http_client.cookies["paperpulse_csrf"]},
        follow_redirects=False,
    )
    assert logout.status_code == 303
    assert "paperpulse_session" not in http_client.cookies
    assert http_client.get("/account").status_code == 401

    with db_factory() as db:
        assert db.scalar(select(User).where(User.email == "new.reader@example.com")) is not None


def test_login_rate_limit_survives_between_requests(http_client, monkeypatch):
    monkeypatch.setattr("api.webapp.send_magic_link", lambda *_args: None)
    csrf = _csrf(http_client)
    for _ in range(5):
        assert http_client.post(
            "/login", data={"csrf_token": csrf, "email": "limited@example.com"}
        ).status_code == 200

    assert http_client.post(
        "/login", data={"csrf_token": csrf, "email": "limited@example.com"}
    ).status_code == 429


def test_account_deletion_removes_identity_and_anonymizes_proposals(http_client, db_factory):
    with db_factory() as db:
        user = User(email="delete-me@example.com")
        db.add(user)
        db.flush()
        topic = create_topic(
            db,
            name="Anonymized proposal",
            description="This proposal remains without its deleted account.",
            categories=["cs.AI"],
            keywords=["privacy testing"],
            creator=user,
        )
        _auth, raw_token = create_auth_session(db, user)
        db.commit()
        user_id, topic_id = user.id, topic.id

    http_client.cookies.set("paperpulse_session", raw_token)
    response = http_client.post(
        "/account/delete",
        data={"csrf_token": _csrf(http_client), "confirmation": "DELETE"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert http_client.get("/account").status_code == 401
    with db_factory() as db:
        assert db.get(User, user_id) is None
        assert db.get(Topic, topic_id).created_by_id is None


def test_anonymous_subscriptions_merge_on_login_and_filter_archived_topics(
    http_client, monkeypatch, db_factory
):
    with db_factory() as db:
        active = _active_topic(db, "Active Robotics")
        archived = _active_topic(db, "Archived Robotics")
        archived.status = "archived"
        existing_user = User(email="subscriber@example.com")
        db.add(existing_user)
        db.flush()
        db.add(Subscription(user_id=existing_user.id, topic_id=archived.id))
        db.commit()
        active_id, archived_id = active.id, archived.id

    csrf = _csrf(http_client)
    response = http_client.post(
        "/subscriptions",
        data={
            "csrf_token": csrf,
            "topic_ids": [str(active_id), str(archived_id)],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    _login_with_magic_link(http_client, monkeypatch, "subscriber@example.com")

    with db_factory() as db:
        user = db.scalar(select(User).where(User.email == "subscriber@example.com"))
        subscribed = set(
            db.scalars(select(Subscription.topic_id).where(Subscription.user_id == user.id))
        )
    assert subscribed == {active_id}

    topics = http_client.get("/topics")
    assert "Active Robotics" in topics.text
    assert "Archived Robotics" not in topics.text


def test_account_only_shows_the_signed_in_users_proposals(http_client, db_factory):
    with db_factory() as db:
        owner = User(email="owner@example.com")
        other = User(email="other@example.com")
        db.add_all([owner, other])
        db.flush()
        own_topic = create_topic(
            db,
            name="Owner-only topic",
            description="A proposal belonging only to the signed-in owner.",
            categories=["cs.AI"],
            keywords=["owner private marker"],
            creator=owner,
        )
        create_topic(
            db,
            name="Other-user topic",
            description="A proposal belonging to a different account.",
            categories=["cs.RO"],
            keywords=["other private marker"],
            creator=other,
        )
        active = _active_topic(db, "Shared active topic")
        propose_terms(db, active, owner, [], ["owner term marker"])
        propose_terms(db, active, other, [], ["other term marker"])
        _auth, raw_token = create_auth_session(db, owner)
        db.commit()
        assert own_topic.id

    http_client.cookies.set("paperpulse_session", raw_token)
    response = http_client.get("/account")
    assert response.status_code == 200
    assert "Owner-only topic" in response.text
    assert "owner term marker" in response.text
    assert "Other-user topic" not in response.text
    assert "other term marker" not in response.text


def test_admin_can_approve_topic_and_reject_term_with_required_reason(
    http_client, db_factory
):
    with db_factory() as db:
        proposer = User(email="proposer@example.com")
        # Configuration, rather than a stale persisted flag, is authoritative.
        admin = User(email=ADMIN_EMAIL, is_admin=False)
        db.add_all([proposer, admin])
        db.flush()
        pending = create_topic(
            db,
            name="Pending Vision",
            description="Computer vision research awaiting moderator approval.",
            categories=["cs.CV"],
            keywords=["visual reasoning"],
            creator=proposer,
        )
        active = _active_topic(db, "Active Vision", creator=admin)
        term = propose_terms(db, active, proposer, [], ["reject this marker"])[0]
        _auth, raw_token = create_auth_session(db, admin)
        db.commit()
        pending_id, term_id = pending.id, term.id

    http_client.cookies.set("paperpulse_session", raw_token)
    csrf = _csrf(http_client)
    missing_reason = http_client.post(
        f"/admin/terms/{term_id}/review",
        data={"csrf_token": csrf, "action": "reject", "reason": ""},
    )
    assert missing_reason.status_code == 400

    approved = http_client.post(
        f"/admin/topics/{pending_id}/review",
        data={"csrf_token": csrf, "action": "approve"},
        follow_redirects=False,
    )
    rejected = http_client.post(
        f"/admin/terms/{term_id}/review",
        data={"csrf_token": csrf, "action": "reject", "reason": "Too broad"},
        follow_redirects=False,
    )
    assert approved.status_code == rejected.status_code == 303

    with db_factory() as db:
        assert db.get(Topic, pending_id).status == "active"
        reviewed_term = db.get(TopicTerm, term_id)
        assert (reviewed_term.status, reviewed_term.review_reason) == ("rejected", "Too broad")
        proposer_id = db.scalar(select(User.id).where(User.email == "proposer@example.com"))
        assert db.get(Subscription, (proposer_id, pending_id)) is not None


def test_report_markdown_is_sanitized_before_rendering(http_client, db_factory):
    with db_factory() as db:
        topic = _active_topic(db, "Safe Rendering")
        report = Report(
            topic_id=topic.id,
            kind="daily",
            title="Untrusted Markdown",
            period_start=date(2026, 7, 16),
            period_end=date(2026, 7, 16),
            content_markdown=(
                "<script>alert('xss')</script>\n\n"
                "[unsafe](javascript:alert('xss'))\n\n"
                "<img src=x onerror=alert(1)>\n\n"
                "[safe](https://example.com/paper)"
            ),
        )
        db.add(report)
        db.commit()
        report_id = report.id

    response = http_client.get(f"/reports/{report_id}")
    assert response.status_code == 200
    assert "<script" not in response.text.casefold()
    assert "javascript:" not in response.text.casefold()
    assert "<img" not in response.text.casefold()
    assert 'href="https://example.com/paper"' in response.text
