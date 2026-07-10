"""
Paper summarisation agent built on the OpenAI Agents SDK.

Two Agent objects are used per run:
  summarizer  — generates a themed summary for each batch of papers
  combiner    — merges multiple batch summaries into one final post

The OPENAI_API_KEY environment variable is read automatically by the SDK.
The model is controlled via the OPENAI_MODEL env var (default: gpt-4o-mini).
"""
import logging
from typing import Any

try:
    from agents import Agent as SDKAgent
    from agents import ModelSettings, Runner
except ImportError:  # pragma: no cover - exercised indirectly in environments without deps
    SDKAgent = None
    ModelSettings = None
    Runner = None

from api.models import Paper
from api.paper_formatter import batch_papers, format_paper
from api.settings import build_combine_prompt, build_summary_prompt, load_app_settings

logger = logging.getLogger(__name__)


class PaperpulseAgent:
    """Orchestrates batch summarisation of ArXiv papers via the OpenAI Agents SDK."""

    def __init__(self, config: dict[str, Any], model: str | None = None):
        if SDKAgent is None or ModelSettings is None:
            raise RuntimeError(
                "openai-agents is not installed. "
                "Install api/requirements.txt before running the pipeline."
            )

        model = model or load_app_settings().openai_model
        self.summarizer = SDKAgent(
            name="Engineering Research Summariser",
            instructions=build_summary_prompt(config),
            model=model,
            model_settings=ModelSettings(
                temperature=0.1,
                top_p=0.9,
            ),
        )
        self.combiner = SDKAgent(
            name="Summary Combiner",
            instructions=build_combine_prompt(config),
            model=model,
            model_settings=ModelSettings(
                temperature=0.1,
                top_p=0.9,
            ),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _format_paper(self, paper: Paper) -> str:
        return format_paper(paper)

    def _batch_papers(self, papers: list[Paper], max_chars: int) -> list[list[Paper]]:
        """Split papers into batches whose combined text stays under max_chars."""
        return batch_papers(papers, max_chars)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def identify_important_papers(self, papers: list[Paper]) -> str:
        """Summarise all papers in batches, then merge into a single post."""
        if not papers:
            raise ValueError("No papers provided to summarise")

        # ~4 chars per token; keep well under the model's context window
        MAX_CHARS = 122_000 * 4

        batches = self._batch_papers(papers, MAX_CHARS)
        intermediate = []

        for i, batch in enumerate(batches, 1):
            logger.info("Processing batch %d / %d", i, len(batches))
            batch_text = "\n".join(self._format_paper(p) for p in batch)
            try:
                result = Runner.run_sync(self.summarizer, batch_text)
                intermediate.append(result.final_output)
                logger.info("Batch %d done", i)
            except Exception as exc:
                logger.error("Batch %d failed: %s", i, exc)

        if not intermediate:
            return ""

        if len(intermediate) == 1:
            return intermediate[0]

        # Merge batch summaries into one coherent post
        combined_text = "\n\n".join(intermediate)
        try:
            result = Runner.run_sync(self.combiner, combined_text)
            return result.final_output
        except Exception as exc:
            logger.error("Combine step failed: %s", exc)
            return combined_text
