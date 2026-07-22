"""One-time import of the original configuration and Jekyll reports."""

import argparse
from datetime import date, datetime
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from api.database import SessionLocal, assert_schema_current
from api.db_models import LegacyRedirect, Report, Topic
from api.settings import load_app_settings, load_config
from api.topic_service import create_topic, normalize


def _read_post(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"Missing front matter in {path}")
    _, front_matter, content = text.split("---", 2)
    return yaml.safe_load(front_matter) or {}, content.strip()


def import_existing(
    topic_name: str | None = None, *, session_factory=SessionLocal
) -> tuple[int, int]:
    settings = load_app_settings()
    config = load_config()
    blog = config.get("blog", {})
    search = config.get("search", {})
    topic_name = (
        topic_name or config.get("topic", {}).get("name") or blog.get("title", "Paperpulse")
    )
    description = blog.get("description") or blog.get("tagline") or "Imported Paperpulse topic"
    with session_factory() as db:
        topic = db.scalar(
            select(Topic)
            .options(selectinload(Topic.terms))
            .where(Topic.name_normalized == normalize(topic_name))
        )
        created = 0
        if not topic:
            topic = create_topic(
                db,
                name=topic_name,
                description=description,
                categories=search.get("categories", []),
                keywords=search.get("keywords", []),
                creator=None,
                status="active",
            )
            db.flush()
            created = 1
        imported = 0
        for path in sorted((settings.project_dir / "blog" / "_posts").glob("*.markdown")):
            front, content = _read_post(path)
            value = front.get("date") or path.name[:10]
            report_date = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
            report = db.scalar(
                select(Report).where(
                    Report.topic_id == topic.id,
                    Report.kind == "daily",
                    Report.period_start == report_date,
                    Report.period_end == report_date,
                )
            )
            if not report:
                report = Report(
                    topic_id=topic.id,
                    kind="daily",
                    title=front.get("title")
                    or f"{topic.name} — Daily report for {report_date:%B %d, %Y}",
                    period_start=report_date,
                    period_end=report_date,
                    content_markdown=content,
                    num_papers=int(front.get("num_papers") or 0),
                    published_at=datetime.combine(report_date, datetime.min.time()),
                )
                db.add(report)
                db.flush()
                imported += 1
            old_path = f"/summary/{report_date:%Y/%m/%d}/{path.stem[11:]}.html"
            if not db.scalar(select(LegacyRedirect).where(LegacyRedirect.old_path == old_path)):
                db.add(LegacyRedirect(old_path=old_path, report_id=report.id))
        db.commit()
    return created, imported


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic-name")
    args = parser.parse_args()
    assert_schema_current()
    created, imported = import_existing(args.topic_name)
    print(f"Created {created} topic; imported {imported} reports")


if __name__ == "__main__":
    main()
