"""Concurrency and retry coverage for durable report job claims."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from api.database import create_db_engine, create_schema
from api.db_models import JobRun, Report, utcnow
from api.pipeline import JOB_STALE_AFTER, _claim_job, run_daily
from api.settings import AppSettings
from api.topic_service import create_topic


def add_active_topic(db, name="AI Engineering"):
    return create_topic(
        db,
        name=name,
        description="Research on artificial intelligence applied to engineering.",
        categories=["cs.AI", "cs.RO"],
        keywords=["engineering automation", "generative design"],
        creator=None,
        status="active",
    )


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


def test_only_one_worker_can_claim_same_job(tmp_path):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'claims.db').as_posix()}")
    create_schema(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        topic = add_active_topic(db)
        db.commit()
        topic_id = topic.id

    barrier = Barrier(2)

    def claim():
        with factory() as db:
            barrier.wait()
            return _claim_job(db, topic_id, "daily", date(2026, 7, 15), date(2026, 7, 15))

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(lambda _index: claim(), range(2)))

    assert sum(value is not None for value in claims) == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(JobRun)) == 1
        assert db.scalar(select(JobRun.attempt_count)) == 1


def test_stale_running_job_is_reclaimed(db_factory):
    report_day = date(2026, 7, 15)
    with db_factory() as db:
        topic = add_active_topic(db)
        db.flush()
        db.add(
            JobRun(
                topic_id=topic.id,
                kind="daily",
                period_start=report_day,
                period_end=report_day,
                status="running",
                attempt_count=1,
                claim_token="abandoned",
                started_at=utcnow() - JOB_STALE_AFTER - timedelta(seconds=1),
                last_attempt_at=utcnow() - JOB_STALE_AFTER - timedelta(seconds=1),
            )
        )
        db.commit()
        claim = _claim_job(db, topic.id, "daily", report_day, report_day)

    assert claim is not None
    with db_factory() as db:
        job = db.scalar(select(JobRun))
        assert job.status == "running"
        assert job.attempt_count == 2
        assert job.claim_token == claim.token


def test_failed_topic_retries_without_blocking_other_topics(monkeypatch, db_factory, sample_paper):
    with db_factory() as db:
        first = add_active_topic(db, "Failure then recovery")
        second = add_active_topic(db, "Always succeeds")
        db.commit()
        first_id = first.id
        second_id = second.id

    class FakeArxiv:
        def __init__(self, query, *_args):
            self.query = query

        def retrieve_daily_results(self, now=None):
            return [sample_paper]

    class FakeBackend:
        fail_first = True

        def identify_important_papers(self, papers):
            if "failure then recovery" in self.query.lower() and self.fail_first:
                FakeBackend.fail_first = False
                raise RuntimeError("temporary backend outage")
            return "## Result\nTest Paper Title"

    def backend(config, _settings):
        value = FakeBackend()
        value.query = config["topic"]["name"]
        return value

    monkeypatch.setattr("api.pipeline.ArxivClient", FakeArxiv)
    monkeypatch.setattr("api.pipeline.create_summary_backend", backend)
    monkeypatch.setattr("api.pipeline.load_config", lambda: {"summarization": {}})
    monkeypatch.setattr(
        "api.pipeline.load_app_settings",
        lambda: AppSettings(project_env="test", project_dir=Path.cwd(), openai_model="unused"),
    )
    now = datetime(2026, 7, 16, 6, tzinfo=UTC)

    first_run = run_daily(now=now, session_factory=db_factory)
    retry = run_daily(now=now, session_factory=db_factory)

    assert (first_run.failed, first_run.succeeded) == (1, 1)
    assert (retry.succeeded, retry.skipped, retry.failed) == (1, 1, 0)
    with db_factory() as db:
        failed_then_retried = db.scalar(
            select(JobRun).where(JobRun.topic_id == first_id)
        )
        other = db.scalar(select(JobRun).where(JobRun.topic_id == second_id))
        assert failed_then_retried.status == "success"
        assert failed_then_retried.attempt_count == 2
        assert other.status == "success"
        assert db.scalar(select(func.count()).select_from(Report)) == 2
