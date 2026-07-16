"""Server-rendered multi-user Paperpulse web application."""

import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import bleach
import markdown
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session, selectinload

from api.auth import (
    allow_login_request,
    allow_proposal,
    consume_magic_link,
    create_auth_session,
    get_auth_session,
    issue_magic_link,
    send_magic_link,
)
from api.database import assert_schema_current, engine, get_db, schema_revisions
from api.db_models import (
    AuthSession,
    JobRun,
    LegacyRedirect,
    Report,
    Subscription,
    Topic,
    TopicTerm,
    User,
)
from api.settings import load_app_settings, load_config
from api.topic_service import create_topic, propose_terms, review_term, review_topic, split_values

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=ROOT / "templates")
settings = load_app_settings()
selection_signer = URLSafeSerializer(settings.session_secret, salt="paperpulse-selections")


def _markdown(value: str) -> str:
    rendered = markdown.markdown(value, extensions=["extra", "sane_lists"])
    return bleach.clean(
        rendered,
        tags={
            "p",
            "br",
            "strong",
            "em",
            "a",
            "ul",
            "ol",
            "li",
            "blockquote",
            "code",
            "pre",
            "h1",
            "h2",
            "h3",
            "h4",
            "hr",
            "table",
            "thead",
            "tbody",
            "tr",
            "th",
            "td",
        },
        attributes={"a": ["href", "title", "target", "rel"]},
        protocols={"http", "https"},
    )


templates.env.filters["render_markdown"] = _markdown


@asynccontextmanager
async def lifespan(_app: FastAPI):
    assert_schema_current()
    yield


app = FastAPI(title="Paperpulse", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def csrf_cookie(request: Request, call_next):  # noqa: ANN001
    csrf = request.cookies.get("paperpulse_csrf") or secrets.token_urlsafe(24)
    request.state.csrf_token = csrf
    response = await call_next(request)
    if "paperpulse_csrf" not in request.cookies:
        response.set_cookie(
            "paperpulse_csrf",
            csrf,
            max_age=31_536_000,
            secure=settings.session_cookie_secure,
            httponly=False,
            samesite="lax",
        )
    return response


def _check_csrf(request: Request, submitted: str) -> None:
    expected = request.cookies.get("paperpulse_csrf") or request.state.csrf_token
    if not secrets.compare_digest(expected, submitted or ""):
        raise HTTPException(403, "Invalid CSRF token")


def _auth(db: Session, request: Request) -> AuthSession | None:
    return get_auth_session(db, request.cookies.get("paperpulse_session"))


def _require_user(db: Session, request: Request) -> User:
    auth = _auth(db, request)
    if not auth:
        raise HTTPException(401, "Sign in required")
    return auth.user


def _require_admin(db: Session, request: Request) -> User:
    user = _require_user(db, request)
    if user.email not in settings.admin_emails:
        raise HTTPException(403, "Administrator access required")
    return user


def _selected_cookie(request: Request) -> set[int]:
    raw = request.cookies.get("paperpulse_topics")
    if not raw:
        return set()
    try:
        values = selection_signer.loads(raw)
        return {int(value) for value in values}
    except (BadSignature, TypeError, ValueError):
        return set()


def _selected_topics(db: Session, request: Request) -> set[int]:
    selected = _selected_cookie(request)
    auth = _auth(db, request)
    if auth:
        selected |= set(
            db.scalars(select(Subscription.topic_id).where(Subscription.user_id == auth.user_id))
        )
    return selected


def _set_selection_cookie(response, topic_ids: set[int]) -> None:  # noqa: ANN001
    response.set_cookie(
        "paperpulse_topics",
        selection_signer.dumps(sorted(topic_ids)),
        max_age=31_536_000,
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _context(db: Session, request: Request, **values) -> dict:  # noqa: ANN003
    auth = _auth(db, request)
    config = load_config()
    return {
        "request": request,
        "user": auth.user if auth else None,
        "is_admin": bool(auth and auth.user.email in settings.admin_emails),
        "csrf_token": request.state.csrf_token,
        "site": config.get("blog", {}),
        **values,
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/ready")
def readiness() -> dict:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        current, expected = schema_revisions()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, "Database readiness check failed") from exc
    if current != expected:
        raise HTTPException(503, "Database migration is required")
    return {"status": "ready", "database_revision": current}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    topics = list(db.scalars(select(Topic).where(Topic.status == "active").order_by(Topic.name)))
    selected = _selected_topics(db, request) & {topic.id for topic in topics}
    reports = []
    if selected:
        reports = list(
            db.scalars(
                select(Report)
                .options(selectinload(Report.topic))
                .where(Report.topic_id.in_(selected))
                .order_by(Report.published_at.desc())
                .limit(100)
            )
        )
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context=_context(db, request, topics=topics, selected=selected, reports=reports),
    )


@app.get("/topics", response_class=HTMLResponse)
def topics_page(request: Request, db: Session = Depends(get_db)):
    topics = list(
        db.scalars(
            select(Topic)
            .options(selectinload(Topic.terms))
            .where(Topic.status == "active")
            .order_by(Topic.name)
        )
    )
    return templates.TemplateResponse(
        request=request,
        name="topics.html",
        context=_context(db, request, topics=topics, selected=_selected_topics(db, request)),
    )


@app.post("/subscriptions")
def update_subscriptions(
    request: Request,
    csrf_token: str = Form(...),
    topic_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    _check_csrf(request, csrf_token)
    valid = set(
        db.scalars(select(Topic.id).where(Topic.status == "active", Topic.id.in_(topic_ids)))
    )
    auth = _auth(db, request)
    if auth:
        db.execute(delete(Subscription).where(Subscription.user_id == auth.user_id))
        db.add_all(Subscription(user_id=auth.user_id, topic_id=topic_id) for topic_id in valid)
        db.commit()
    response = RedirectResponse("/", status_code=303)
    _set_selection_cookie(response, valid)
    return response


@app.get("/topics/propose", response_class=HTMLResponse)
def propose_topic_page(request: Request, db: Session = Depends(get_db)):
    _require_user(db, request)
    return templates.TemplateResponse(
        request=request, name="propose_topic.html", context=_context(db, request, error=None)
    )


@app.post("/topics/propose", response_class=HTMLResponse)
def propose_topic_action(
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(...),
    description: str = Form(...),
    categories: str = Form(...),
    keywords: str = Form(...),
    db: Session = Depends(get_db),
):
    _check_csrf(request, csrf_token)
    user = _require_user(db, request)
    if not allow_proposal(db, user.id):
        raise HTTPException(429, "Proposal limit reached; try again tomorrow")
    try:
        create_topic(
            db,
            name=name,
            description=description,
            categories=split_values(categories),
            keywords=split_values(keywords),
            creator=user,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return templates.TemplateResponse(
            request=request,
            name="propose_topic.html",
            context=_context(
                db,
                request,
                error=str(exc),
                values={
                    "name": name,
                    "description": description,
                    "categories": categories,
                    "keywords": keywords,
                },
            ),
            status_code=400,
        )
    return RedirectResponse("/account", status_code=303)


@app.get("/topics/{slug}", response_class=HTMLResponse)
def topic_detail(slug: str, request: Request, page: int = 1, db: Session = Depends(get_db)):
    page = max(page, 1)
    page_size = 25
    topic = db.scalar(
        select(Topic)
        .options(selectinload(Topic.terms))
        .where(Topic.slug == slug, Topic.status == "active")
    )
    if not topic:
        raise HTTPException(404)
    reports = list(
        db.scalars(
            select(Report)
            .where(Report.topic_id == topic.id)
            .order_by(Report.published_at.desc(), Report.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size + 1)
        )
    )
    has_next = len(reports) > page_size
    return templates.TemplateResponse(
        request=request,
        name="topic_detail.html",
        context=_context(
            db,
            request,
            topic=topic,
            reports=reports[:page_size],
            page=page,
            has_next=has_next,
            error=None,
        ),
    )


@app.post("/topics/{slug}/suggest", response_class=HTMLResponse)
def suggest_terms(
    slug: str,
    request: Request,
    csrf_token: str = Form(...),
    categories: str = Form(default=""),
    keywords: str = Form(default=""),
    db: Session = Depends(get_db),
):
    _check_csrf(request, csrf_token)
    user = _require_user(db, request)
    if not allow_proposal(db, user.id):
        raise HTTPException(429, "Proposal limit reached; try again tomorrow")
    topic = db.scalar(
        select(Topic)
        .options(selectinload(Topic.terms))
        .where(Topic.slug == slug, Topic.status == "active")
    )
    if not topic:
        raise HTTPException(404)
    try:
        propose_terms(db, topic, user, split_values(categories), split_values(keywords))
        db.commit()
    except ValueError as exc:
        db.rollback()
        return templates.TemplateResponse(
            request=request,
            name="topic_detail.html",
            context=_context(
                db,
                request,
                topic=topic,
                reports=list(
                    db.scalars(
                        select(Report)
                        .where(Report.topic_id == topic.id)
                        .order_by(Report.published_at.desc(), Report.id.desc())
                        .limit(25)
                    )
                ),
                page=1,
                has_next=False,
                error=str(exc),
            ),
            status_code=400,
        )
    return RedirectResponse("/account", status_code=303)


@app.get("/reports/{report_id}", response_class=HTMLResponse)
def report_detail(report_id: int, request: Request, db: Session = Depends(get_db)):
    report = db.scalar(
        select(Report).options(selectinload(Report.topic)).where(Report.id == report_id)
    )
    if not report:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request=request, name="report.html", context=_context(db, request, report=report)
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context=_context(db, request, message=None, debug_url=None),
    )


@app.post("/login", response_class=HTMLResponse)
def request_login(
    request: Request,
    csrf_token: str = Form(...),
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    _check_csrf(request, csrf_token)
    ip = request.client.host if request.client else "unknown"
    try:
        if not allow_login_request(db, email, ip):
            raise HTTPException(429, "Too many sign-in requests")
        link, token = issue_magic_link(db, email, ip)
        send_magic_link(settings, link.email, token)
        db.commit()
    except (ValueError, RuntimeError) as exc:
        db.rollback()
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context=_context(db, request, message=str(exc), debug_url=None),
            status_code=400,
        )
    debug_url = (
        f"{settings.public_base_url}/auth/verify?token={token}"
        if settings.magic_link_debug
        else None
    )
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context=_context(
            db, request, message="Check your email for a sign-in link.", debug_url=debug_url
        ),
    )


@app.get("/auth/verify", response_class=HTMLResponse)
def verify_login_page(token: str, request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request=request,
        name="verify_login.html",
        context=_context(db, request, token=token),
    )


@app.post("/auth/verify")
def verify_login(
    request: Request,
    csrf_token: str = Form(...),
    token: str = Form(...),
    db: Session = Depends(get_db),
):
    _check_csrf(request, csrf_token)
    user = consume_magic_link(db, token, settings)
    if not user:
        raise HTTPException(400, "This sign-in link is invalid or expired")
    db.flush()
    selected = _selected_cookie(request)
    selected |= set(
        db.scalars(select(Subscription.topic_id).where(Subscription.user_id == user.id))
    )
    valid = set(
        db.scalars(select(Topic.id).where(Topic.id.in_(selected), Topic.status == "active"))
    )
    db.execute(
        delete(Subscription).where(
            Subscription.user_id == user.id,
            Subscription.topic_id.not_in(valid),
        )
    )
    for topic_id in valid:
        if not db.get(Subscription, (user.id, topic_id)):
            db.add(Subscription(user_id=user.id, topic_id=topic_id))
    auth_session, raw_token = create_auth_session(db, user)
    db.commit()
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        "paperpulse_session",
        raw_token,
        max_age=2_592_000,
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    _set_selection_cookie(response, valid)
    return response


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form(...), db: Session = Depends(get_db)):
    _check_csrf(request, csrf_token)
    auth = _auth(db, request)
    if auth:
        db.delete(auth)
        db.commit()
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("paperpulse_session")
    return response


@app.get("/account", response_class=HTMLResponse)
def account(request: Request, page: int = 1, db: Session = Depends(get_db)):
    user = _require_user(db, request)
    page = max(page, 1)
    page_size = 25
    topics = list(
        db.scalars(
            select(Topic)
            .options(selectinload(Topic.terms))
            .where(Topic.created_by_id == user.id)
            .order_by(Topic.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size + 1)
        )
    )
    terms = list(
        db.scalars(
            select(TopicTerm)
            .options(selectinload(TopicTerm.topic))
            .where(
                TopicTerm.created_by_id == user.id,
                TopicTerm.topic_id.not_in(
                    select(Topic.id).where(Topic.created_by_id == user.id)
                ),
            )
            .order_by(TopicTerm.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size + 1)
        )
    )
    has_next = len(topics) > page_size or len(terms) > page_size
    return templates.TemplateResponse(
        request=request,
        name="account.html",
        context=_context(
            db,
            request,
            topics=topics[:page_size],
            terms=terms[:page_size],
            page=page,
            has_next=has_next,
        ),
    )


@app.post("/account/delete")
def delete_account(
    request: Request,
    csrf_token: str = Form(...),
    confirmation: str = Form(...),
    db: Session = Depends(get_db),
):
    _check_csrf(request, csrf_token)
    user = _require_user(db, request)
    if confirmation.strip().casefold() != "delete":
        raise HTTPException(400, "Type DELETE to confirm account deletion")
    db.delete(user)
    db.commit()
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("paperpulse_session")
    return response


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request=request, name="privacy.html", context=_context(db, request)
    )


@app.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request=request, name="terms.html", context=_context(db, request)
    )


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, page: int = 1, db: Session = Depends(get_db)):
    _require_admin(db, request)
    page = max(page, 1)
    page_size = 25
    pending_topics = list(
        db.scalars(
            select(Topic)
            .options(selectinload(Topic.terms))
            .where(Topic.status == "pending")
            .order_by(Topic.created_at)
            .offset((page - 1) * page_size)
            .limit(page_size + 1)
        )
    )
    pending_terms = list(
        db.scalars(
            select(TopicTerm)
            .options(selectinload(TopicTerm.topic))
            .where(
                TopicTerm.status == "pending",
                TopicTerm.topic_id.not_in(select(Topic.id).where(Topic.status == "pending")),
            )
            .order_by(TopicTerm.created_at)
            .offset((page - 1) * page_size)
            .limit(page_size + 1)
        )
    )
    active_topics = list(
        db.scalars(select(Topic).where(Topic.status == "active").order_by(Topic.name))
    )
    recent_jobs = list(
        db.scalars(select(JobRun).order_by(JobRun.started_at.desc()).limit(50))
    )
    has_next = len(pending_topics) > page_size or len(pending_terms) > page_size
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context=_context(
            db,
            request,
            pending_topics=pending_topics[:page_size],
            pending_terms=pending_terms[:page_size],
            active_topics=active_topics,
            recent_jobs=recent_jobs,
            page=page,
            has_next=has_next,
            error=None,
        ),
    )


@app.post("/admin/topics")
def admin_create_topic(
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(...),
    description: str = Form(...),
    categories: str = Form(...),
    keywords: str = Form(...),
    db: Session = Depends(get_db),
):
    _check_csrf(request, csrf_token)
    admin = _require_admin(db, request)
    try:
        create_topic(
            db,
            name=name,
            description=description,
            categories=split_values(categories),
            keywords=split_values(keywords),
            creator=admin,
            status="active",
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/topics/{topic_id}/review")
def admin_review_topic(
    topic_id: int,
    request: Request,
    csrf_token: str = Form(...),
    action: str = Form(...),
    reason: str = Form(default=""),
    db: Session = Depends(get_db),
):
    _check_csrf(request, csrf_token)
    admin = _require_admin(db, request)
    topic = db.scalar(select(Topic).options(selectinload(Topic.terms)).where(Topic.id == topic_id))
    if not topic or topic.status != "pending" or action not in {"approve", "reject"}:
        raise HTTPException(400, "Invalid moderation action")
    if action == "reject" and not reason.strip():
        raise HTTPException(400, "A rejection reason is required")
    review_topic(db, topic, admin, action == "approve", reason)
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/terms/{term_id}/review")
def admin_review_term(
    term_id: int,
    request: Request,
    csrf_token: str = Form(...),
    action: str = Form(...),
    reason: str = Form(default=""),
    db: Session = Depends(get_db),
):
    _check_csrf(request, csrf_token)
    admin = _require_admin(db, request)
    term = db.get(TopicTerm, term_id)
    if not term or term.status != "pending" or action not in {"approve", "reject"}:
        raise HTTPException(400, "Invalid moderation action")
    if action == "reject" and not reason.strip():
        raise HTTPException(400, "A rejection reason is required")
    review_term(term, admin, action == "approve", reason)
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/topics/{topic_id}/edit")
def admin_edit_topic(
    topic_id: int,
    request: Request,
    csrf_token: str = Form(...),
    description: str = Form(...),
    categories: str = Form(default=""),
    keywords: str = Form(default=""),
    db: Session = Depends(get_db),
):
    _check_csrf(request, csrf_token)
    admin = _require_admin(db, request)
    topic = db.scalar(select(Topic).options(selectinload(Topic.terms)).where(Topic.id == topic_id))
    if not topic or topic.status != "active":
        raise HTTPException(404)
    topic.description = description.strip()
    try:
        propose_terms(
            db, topic, admin, split_values(categories), split_values(keywords), status="active"
        ) if categories.strip() or keywords.strip() else None
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/topics/{topic_id}/archive")
def admin_archive_topic(
    topic_id: int, request: Request, csrf_token: str = Form(...), db: Session = Depends(get_db)
):
    _check_csrf(request, csrf_token)
    _require_admin(db, request)
    topic = db.get(Topic, topic_id)
    if not topic:
        raise HTTPException(404)
    topic.status = "archived"
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@app.get("/{legacy_path:path}")
def legacy_redirect(legacy_path: str, db: Session = Depends(get_db)):
    redirect = db.scalar(select(LegacyRedirect).where(LegacyRedirect.old_path == f"/{legacy_path}"))
    if not redirect:
        raise HTTPException(404)
    return RedirectResponse(f"/reports/{redirect.report_id}", status_code=301)
