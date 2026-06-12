import logging
from dataclasses import dataclass

from dotenv import load_dotenv

from api.arxiv_client import ArxivClient
from api.file_handler import FileHandler
from api.settings import (
    ARXIV_SORT_BY,
    ARXIV_SORT_ORDER,
    build_arxiv_query,
    load_app_settings,
    load_config,
)
from api.summary_backend import create_summary_backend
from api.utils import add_markdown_links
from api.webs import create_blogpost

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


@dataclass(frozen=True)
class PipelineResult:
    ok: bool
    message: str
    num_papers: int = 0
    post_path: str | None = None


def main() -> PipelineResult:
    """
    Main function that orchestrates the retrieval and summarization process.
    """

    try:
        load_dotenv()
        settings = load_app_settings()
        logger.info("Running Paperpulse in %s mode", settings.project_env)
        logger.info("Using %s LLM backend", settings.llm_backend)

        config = load_config()
        search_query = build_arxiv_query(config)
        logger.info("ArXiv query: %s", search_query)

        arxiv_client = ArxivClient(search_query, ARXIV_SORT_BY, ARXIV_SORT_ORDER)
        llm_agent = create_summary_backend(config, settings)
        file_handler = FileHandler(settings.data_dir)
        papers = None

        if settings.project_env == 'dev':
            papers = file_handler.load_papers()

        if not papers:
            logger.info("Retrieving daily results")
            papers = arxiv_client.retrieve_daily_results()

            if settings.project_env == 'dev':
                file_handler.save_papers(papers)

        if not papers:
            message = "No papers retrieved from ArXiv for the configured query"
            logger.error(message)
            return PipelineResult(ok=False, message=message)

        logger.info("Retrieved %d papers", len(papers))

        summary = llm_agent.identify_important_papers(papers)
        if not summary.strip():
            message = "LLM backend returned an empty summary"
            logger.error(message)
            return PipelineResult(ok=False, message=message, num_papers=len(papers))

        logger.info("Generated summary with %d characters", len(summary))

        summary_linked = add_markdown_links(summary, papers)
        post_path = create_blogpost(summary_linked, len(papers), config, settings=settings)
        message = f"Created blog post from {len(papers)} papers: {post_path.name}"
        logger.info(message)
        return PipelineResult(
            ok=True,
            message=message,
            num_papers=len(papers),
            post_path=str(post_path),
        )
    except Exception as e:
        logger.exception("Paperpulse pipeline failed: %s", e)
        return PipelineResult(ok=False, message=f"{type(e).__name__}: {e}")

if __name__ == "__main__":
    main()
