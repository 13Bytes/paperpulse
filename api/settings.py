import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import yaml

# ---------------------------------------------------------------------------
# OpenAI settings (read from environment — OPENAI_API_KEY is read by the SDK)
# ---------------------------------------------------------------------------
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# ArXiv sort parameters (rarely need changing)
ARXIV_SORT_BY = 'lastUpdatedDate'
ARXIV_SORT_ORDER = 'descending'


@dataclass(frozen=True)
class AppSettings:
    """Runtime settings derived from environment variables."""

    project_env: str
    project_dir: Path
    openai_model: str
    llm_backend: Literal["openai_api", "codex_cli"] = "openai_api"
    codex_home: Path | None = None
    codex_model: str | None = None
    codex_timeout_seconds: int = 900
    manual_triggers_allowed: bool = False

    @property
    def data_dir(self) -> Path:
        return self.project_dir / "data"

    @property
    def posts_dir(self) -> Path:
        return self.project_dir / "blog" / "_posts"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def parse_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def load_app_settings() -> AppSettings:
    project_dir = Path(os.getenv("PROJECT_DIR") or repo_root()).expanduser().resolve()
    llm_backend = os.getenv("LLM_BACKEND", "openai_api").strip().lower()
    if llm_backend not in {"openai_api", "codex_cli"}:
        raise ValueError(
            "LLM_BACKEND must be one of: openai_api, codex_cli; "
            f"got {llm_backend!r}."
        )
    codex_home = os.getenv("CODEX_HOME")
    codex_timeout = os.getenv("CODEX_TIMEOUT_SECONDS", "900")
    try:
        codex_timeout_seconds = int(codex_timeout)
    except ValueError as exc:
        raise ValueError("CODEX_TIMEOUT_SECONDS must be an integer") from exc
    if codex_timeout_seconds <= 0:
        raise ValueError("CODEX_TIMEOUT_SECONDS must be greater than zero")

    return AppSettings(
        project_env=os.getenv("PROJECT_ENV", "dev").strip().lower(),
        project_dir=project_dir,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        llm_backend=cast(Literal["openai_api", "codex_cli"], llm_backend),
        codex_home=Path(codex_home).expanduser().resolve() if codex_home else None,
        codex_model=(os.getenv("CODEX_MODEL") or "").strip() or None,
        codex_timeout_seconds=codex_timeout_seconds,
        manual_triggers_allowed=parse_bool(os.getenv("MANUAL_TRIGGERS_ALLOWED")),
    )


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(config_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """
    Load paperpulse configuration from config.yaml.

    Searches (in order):
      1. The explicit ``config_path`` argument
      2. ``$PROJECT_DIR/config.yaml``
      3. The repository root (two directories above this file)
    """
    if config_path is None:
        repo_root = os.path.join(os.path.dirname(__file__), "..")
        candidates = [
            os.path.join(os.getenv("PROJECT_DIR", ""), "config.yaml"),
            os.path.join(repo_root, "config.yaml"),
        ]
        for path in candidates:
            if os.path.isfile(path):
                config_path = path
                break

    if not config_path or not Path(config_path).is_file():
        raise FileNotFoundError(
            "config.yaml not found. Set PROJECT_DIR or place config.yaml at the repo root."
        )

    with Path(config_path).open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# ArXiv query builder
# ---------------------------------------------------------------------------

def build_arxiv_query(config: dict[str, Any]) -> str:
    """
    Construct an ArXiv API ``search_query`` string from *config*.

    The query format uses URL-encoded Boolean syntax:
    - Categories joined with ``+OR+`` (e.g. ``cat:cs.SE+OR+cat:cs.RO``)
    - Keywords mapped to ``all:`` prefix; multi-word terms are quoted
    - Both sides wrapped in parentheses and joined with ``+AND+`` when
      ``search.mode`` is ``categories_and_keywords`` (the default)
    """
    search = config.get("search", {})
    categories = search.get("categories", [])
    keywords = search.get("keywords", [])
    mode = search.get("mode", "categories_and_keywords")

    def _encode_keyword(kw: str) -> str:
        parts = kw.strip().split()
        if not parts:
            return ""
        if len(parts) > 1:
            return "all:%22" + "+".join(parts) + "%22"
        return f"all:{parts[0]}"

    cat_query = "+OR+".join(f"cat:{c}" for c in categories) if categories else ""
    encoded_keywords = [_encode_keyword(kw) for kw in keywords]
    kw_query = "+OR+".join(kw for kw in encoded_keywords if kw) if keywords else ""

    if mode == "categories_only" or not kw_query:
        return cat_query or "cat:cs.AI"
    if mode == "keywords_only" or not cat_query:
        return kw_query or "cat:cs.AI"
    # categories_and_keywords (default)
    return f"%28{cat_query}%29+AND+%28{kw_query}%29"


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def build_summary_prompt(config: dict[str, Any]) -> str:
    """Return the system prompt used for per-batch summarisation."""
    summarization = config.get("summarization", {})
    persona = summarization.get("persona", "You are a research scientist.")
    style = summarization.get("style", "Explain concepts clearly and precisely.")
    return f"""{persona}
{style}

You are provided with a collection of academic papers and their abstracts.
Your goal is to write a single, coherent blogpost-style summary (under 5000 words) \
that summarises key developments and groups these into major themes.

For each theme:
1. Use a clear, descriptive heading (e.g., "Theme 1: Agentic Design Pipelines")
2. Highlight the most important developments and insights within that theme
3. Mention specific papers when relevant to illustrate points
4. If you mention specific papers, make sure to mention the complete title
5. Show how papers within the theme connect to each other

Write about each theme starting directly with "Theme 1:".
Do not include any introductory text before Theme 1.

Format:
## Theme 1: [Theme Name]
[Content about theme 1 papers]

## Theme 2: [Theme Name]
[Content about theme 2 papers]

And so on...

List of Papers and Abstracts:
"""


def build_combine_prompt(config: dict[str, Any]) -> str:
    """Return the prompt used to merge multiple batch summaries into one."""
    summarization = config.get("summarization", {})
    persona = summarization.get("persona", "You are a research scientist.")
    style = summarization.get("style", "Explain concepts clearly and precisely.")
    return f"""{persona}
{style}

You are tasked with combining multiple research summaries into a single coherent summary.
Please combine the following summaries, maintaining the thematic organisation
and removing any redundancy.
Write about each theme starting directly with "Theme 1:".
Do not include any introductory text before Theme 1.

"""
