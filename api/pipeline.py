"""Per-topic daily and weekly report generation."""

import copy
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
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


@dataclass(frozen=True)
class BatchResult:
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0


def topic_config(topic: Topic, base_config: dict) -> dict:
    config = copy.deepcopy(base_config)
    config["topic"] = {"name": topic.name, "description": topic.description}
    config["search"] = {
        "categories": active_term_values(topic, "category"),
        "keywords": active_term_values(topic, "keyword"),
        "mode": "categories_and_keywords",
    }
    return config


def _job(db: Session, topic_id: int, kind: str, start: date, end: date) -> JobRun:
    job = db.scalar(
        select(JobRun).where(
            JobRun.topic_id == topic_id,
            JobRun.kind == kind,
            JobRun.period_start == start,
            JobRun.period_end == end,
        )
    )
    if not job:
        job = JobRun(
            topic_id=topic_id, kind=kind, period_start=start, period_end=end, status="running"
        )
        db.add(job)
    else:
        job.status = "running"
        job.message = None
        job.started_at = utcnow()
        job.finished_at = None
    db.flush()
    return job


def _finish(job: JobRun, status: str, message: str) -> None:
    job.status = status
    job.message = message
    job.finished_at = utcnow()


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
            job = _job(db, topic_id, "daily", report_day, report_day)
            try:
                config = topic_config(topic, base_config)
                query = build_arxiv_query(config)
                papers = ArxivClient(query, ARXIV_SORT_BY, ARXIV_SORT_ORDER).retrieve_daily_results(
                    now=now
                )
                if not papers:
                    _finish(job, "skipped", "No matching papers")
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
                _finish(job, "success", f"Published report from {len(papers)} papers")
                db.commit()
                counts["succeeded"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception("Daily report failed for topic %s", topic_id)
                db.rollback()
                job = _job(db, topic_id, "daily", report_day, report_day)
                _finish(job, "failed", f"{type(exc).__name__}: {exc}")
                db.commit()
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
            job = _job(db, topic_id, "weekly", period_start, period_end)
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
                    _finish(job, "skipped", "No daily reports in the weekly period")
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
                _finish(job, "success", f"Published from {len(daily_reports)} daily reports")
                db.commit()
                counts["succeeded"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception("Weekly report failed for topic %s", topic_id)
                db.rollback()
                job = _job(db, topic_id, "weekly", period_start, period_end)
                _finish(job, "failed", f"{type(exc).__name__}: {exc}")
                db.commit()
                counts["failed"] += 1
    return BatchResult(**counts)
