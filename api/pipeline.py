"""Per-topic daily and weekly report generation."""

import copy
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from api.arxiv_client import ArxivClient
from api.database import SessionLocal
from api.db_models import JobRun, Report, Topic, utcnow
from api.settings import (
    ARXIV_SORT_BY,
    ARXIV_SORT_ORDER,
    build_arxiv_query,
    load_app_settings,
    load_config,
)
from api.summary_backend import create_summary_backend
from api.topic_service import active_term_values
from api.utils import add_markdown_links

logger = logging.getLogger(__name__)
JOB_STALE_AFTER = timedelta(hours=2)


@dataclass(frozen=True)
class BatchResult:
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass(frozen=True)
class JobClaim:
    id: int
    token: str


class LostJobClaim(RuntimeError):
    """Raised when a stale worker tries to finish a job claimed by another worker."""


def topic_config(topic: Topic, base_config: dict) -> dict:
    config = copy.deepcopy(base_config)
    config["topic"] = {"name": topic.name, "description": topic.description}
    config["search"] = {
        "categories": active_term_values(topic, "category"),
        "keywords": active_term_values(topic, "keyword"),
        "mode": "categories_and_keywords",
    }
    return config


def _claim_job(
    db: Session, topic_id: int, kind: str, start: date, end: date
) -> JobClaim | None:
    """Atomically claim a new, failed, or stale job and publish the claim immediately."""
    now = utcnow()
    token = str(uuid.uuid4())
    job = JobRun(
        topic_id=topic_id,
        kind=kind,
        period_start=start,
        period_end=end,
        status="running",
        attempt_count=1,
        claim_token=token,
        started_at=now,
        last_attempt_at=now,
    )
    db.add(job)
    try:
        db.commit()
        logger.info(
            "Claimed %s job %s for topic %s (%s to %s), attempt 1",
            kind,
            job.id,
            topic_id,
            start,
            end,
        )
        return JobClaim(job.id, token)
    except IntegrityError:
        db.rollback()

    stale_before = now - JOB_STALE_AFTER
    result = db.execute(
        update(JobRun)
        .where(
            JobRun.topic_id == topic_id,
            JobRun.kind == kind,
            JobRun.period_start == start,
            JobRun.period_end == end,
            or_(
                JobRun.status == "failed",
                and_(JobRun.status == "running", JobRun.started_at <= stale_before),
            ),
        )
        .values(
            status="running",
            message=None,
            attempt_count=JobRun.attempt_count + 1,
            claim_token=token,
            started_at=now,
            last_attempt_at=now,
            finished_at=None,
        )
    )
    if result.rowcount != 1:
        db.rollback()
        logger.info(
            "Skipped already claimed or completed %s job for topic %s (%s to %s)",
            kind,
            topic_id,
            start,
            end,
        )
        return None
    claimed_job = db.execute(
        select(JobRun.id, JobRun.attempt_count).where(
            JobRun.topic_id == topic_id,
            JobRun.kind == kind,
            JobRun.period_start == start,
            JobRun.period_end == end,
        )
    ).one()
    db.commit()
    logger.info(
        "Reclaimed %s job %s for topic %s (%s to %s), attempt %s",
        kind,
        claimed_job.id,
        topic_id,
        start,
        end,
        claimed_job.attempt_count,
    )
    return JobClaim(claimed_job.id, token)


def _finish(db: Session, claim: JobClaim, status: str, message: str) -> None:
    result = db.execute(
        update(JobRun)
        .where(
            JobRun.id == claim.id,
            JobRun.claim_token == claim.token,
            JobRun.status == "running",
        )
        .values(status=status, message=message, finished_at=utcnow(), claim_token=None)
    )
    if result.rowcount != 1:
        raise LostJobClaim(f"Job {claim.id} is no longer owned by this worker")
    logger.info("Finished job %s with status %s: %s", claim.id, status, message)


def run_daily(*, now: datetime | None = None, session_factory=SessionLocal) -> BatchResult:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    report_day = now.date() - timedelta(days=1)
    base_config = load_config()
    settings = load_app_settings()
    counts = {"succeeded": 0, "skipped": 0, "failed": 0}
    with session_factory() as db:
        topic_ids = list(
            db.scalars(select(Topic.id).where(Topic.status == "active").order_by(Topic.id))
        )
    for topic_id in topic_ids:
        with session_factory() as db:
            topic = db.scalar(
                select(Topic).options(selectinload(Topic.terms)).where(Topic.id == topic_id)
            )
            existing = db.scalar(
                select(Report.id).where(
                    Report.topic_id == topic_id,
                    Report.kind == "daily",
                    Report.period_start == report_day,
                    Report.period_end == report_day,
                )
            )
            if existing:
                counts["skipped"] += 1
                continue
            claim = _claim_job(db, topic_id, "daily", report_day, report_day)
            if claim is None:
                counts["skipped"] += 1
                continue
            try:
                config = topic_config(topic, base_config)
                query = build_arxiv_query(config)
                client = ArxivClient(query, ARXIV_SORT_BY, ARXIV_SORT_ORDER)
                papers = client.retrieve_daily_results(now=now)
                if not papers:
                    _finish(db, claim, "skipped", "No matching papers")
                    db.commit()
                    counts["skipped"] += 1
                    continue
                backend = create_summary_backend(config, settings)
                content = backend.identify_important_papers(papers)
                if not content.strip():
                    raise RuntimeError("Summary backend returned an empty report")
                content = add_markdown_links(content, papers)
                db.add(
                    Report(
                        topic_id=topic.id,
                        kind="daily",
                        title=f"{topic.name} — Daily report for {report_day:%B %d, %Y}",
                        period_start=report_day,
                        period_end=report_day,
                        content_markdown=content,
                        num_papers=len(papers),
                    )
                )
                _finish(db, claim, "success", f"Published report from {len(papers)} papers")
                db.commit()
                counts["succeeded"] += 1
            except LostJobClaim:
                logger.warning("Daily job claim was lost for topic %s", topic_id)
                db.rollback()
                counts["skipped"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception("Daily report failed for topic %s", topic_id)
                db.rollback()
                try:
                    _finish(db, claim, "failed", f"{type(exc).__name__}: {exc}")
                    db.commit()
                except LostJobClaim:
                    db.rollback()
                counts["failed"] += 1
    return BatchResult(**counts)


def run_weekly(*, as_of: date | None = None, session_factory=SessionLocal) -> BatchResult:
    as_of = as_of or datetime.now(UTC).date()
    period_end = as_of - timedelta(days=1)
    period_start = period_end - timedelta(days=6)
    base_config = load_config()
    settings = load_app_settings()
    counts = {"succeeded": 0, "skipped": 0, "failed": 0}
    with session_factory() as db:
        topic_ids = list(
            db.scalars(select(Topic.id).where(Topic.status == "active").order_by(Topic.id))
        )
    for topic_id in topic_ids:
        with session_factory() as db:
            topic = db.scalar(
                select(Topic).options(selectinload(Topic.terms)).where(Topic.id == topic_id)
            )
            existing = db.scalar(
                select(Report.id).where(
                    Report.topic_id == topic_id,
                    Report.kind == "weekly",
                    Report.period_start == period_start,
                    Report.period_end == period_end,
                )
            )
            if existing:
                counts["skipped"] += 1
                continue
            claim = _claim_job(db, topic_id, "weekly", period_start, period_end)
            if claim is None:
                counts["skipped"] += 1
                continue
            try:
                daily_reports = list(
                    db.scalars(
                        select(Report)
                        .where(
                            Report.topic_id == topic_id,
                            Report.kind == "daily",
                            Report.period_start >= period_start,
                            Report.period_end <= period_end,
                        )
                        .order_by(Report.period_start)
                    )
                )
                if not daily_reports:
                    _finish(db, claim, "skipped", "No daily reports in the weekly period")
                    db.commit()
                    counts["skipped"] += 1
                    continue
                config = topic_config(topic, base_config)
                content = create_summary_backend(config, settings).summarize_weekly(
                    [report.content_markdown for report in daily_reports]
                )
                if not content.strip():
                    raise RuntimeError("Summary backend returned an empty weekly report")
                weekly = Report(
                    topic_id=topic.id,
                    kind="weekly",
                    title=f"{topic.name} — Week of {period_start:%B %d, %Y}",
                    period_start=period_start,
                    period_end=period_end,
                    content_markdown=content,
                    num_papers=sum(report.num_papers for report in daily_reports),
                )
                weekly.source_reports.extend(daily_reports)
                db.add(weekly)
                _finish(
                    db, claim, "success", f"Published from {len(daily_reports)} daily reports"
                )
                db.commit()
                counts["succeeded"] += 1
            except LostJobClaim:
                logger.warning("Weekly job claim was lost for topic %s", topic_id)
                db.rollback()
                counts["skipped"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception("Weekly report failed for topic %s", topic_id)
                db.rollback()
                try:
                    _finish(db, claim, "failed", f"{type(exc).__name__}: {exc}")
                    db.commit()
                except LostJobClaim:
                    db.rollback()
                counts["failed"] += 1
    return BatchResult(**counts)
