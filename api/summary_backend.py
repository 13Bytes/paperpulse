from typing import Any, Protocol

from api.agent import PaperpulseAgent
from api.codex_agent import CodexCliAgent
from api.models import Paper
from api.settings import AppSettings


class SummaryBackend(Protocol):
    def identify_important_papers(self, papers: list[Paper]) -> str:
        """Return a Markdown summary for the supplied papers."""

    def summarize_weekly(self, daily_reports: list[str]) -> str:
        """Return a weekly Markdown synthesis of daily reports."""


def create_summary_backend(config: dict[str, Any], settings: AppSettings) -> SummaryBackend:
    if settings.llm_backend == "openai_api":
        return PaperpulseAgent(config, model=settings.openai_model)
    if settings.llm_backend == "codex_cli":
        return CodexCliAgent(config, settings)
    raise ValueError(
        "Unsupported LLM_BACKEND. Expected one of: openai_api, codex_cli; "
        f"got {settings.llm_backend!r}."
    )
