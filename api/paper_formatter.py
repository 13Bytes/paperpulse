import logging

from api.models import Paper

logger = logging.getLogger(__name__)


def format_paper(paper: Paper) -> str:
    return (
        f"**Title:** {paper['title']}\n"
        f"**Authors:** {', '.join(paper['authors'])}\n"
        f"**Summary:** {paper['summary']}\n"
    )


def batch_papers(papers: list[Paper], max_chars: int) -> list[list[Paper]]:
    """Split papers into batches whose combined text stays under max_chars."""
    batches, current_batch, current_length = [], [], 0
    for paper in papers:
        chunk = format_paper(paper)
        if current_length + len(chunk) > max_chars and current_batch:
            batches.append(current_batch)
            current_batch, current_length = [], 0
        current_batch.append(paper)
        current_length += len(chunk)
    if current_batch:
        batches.append(current_batch)
    logger.info("Split %d papers into %d batches", len(papers), len(batches))
    return batches
