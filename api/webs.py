"""Create Jekyll blog posts from generated summaries."""

from datetime import datetime
from pathlib import Path

import yaml

from api.settings import AppSettings, load_app_settings


def create_blogpost(
    summary: str,
    num_papers: int,
    config: dict | None = None,
    settings: AppSettings | None = None,
    date: datetime | None = None,
    output_dir: str | Path | None = None,
) -> Path:
    """
    Creates a markdown file with specified naming convention and writes content.

    Args:
        summary (str): The summary content to write
        num_papers (int): Number of papers included in the summary
        config (dict): Paperpulse config loaded from config.yaml (optional)
    """
    if config is None:
        config = {}
    if settings is None:
        settings = load_app_settings()

    blog_cfg = config.get("blog", {})
    post_title = blog_cfg.get("post_title", "Daily Research Summary")

    todays_date = (date or datetime.now()).strftime('%Y-%m-%d')
    filename = f"{todays_date}-daily-summary.markdown"

    front_matter = yaml.safe_dump(
        {
            "layout": "post",
            "title": post_title,
            "date": todays_date,
            "categories": "summary",
            "num_papers": num_papers,
        },
        sort_keys=False,
        allow_unicode=True,
    ).strip()
    header = f"---\n{front_matter}\n---\n"
    full_content = f"{header}\n\n{summary}"

    posts_dir = Path(output_dir) if output_dir is not None else settings.posts_dir
    posts_dir.mkdir(parents=True, exist_ok=True)
    post_path = posts_dir / filename
    post_path.write_text(full_content, encoding="utf-8")
    return post_path
