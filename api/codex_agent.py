import json
import logging
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from api.models import Paper
from api.paper_formatter import batch_papers, format_paper
from api.settings import (
    AppSettings,
    build_combine_prompt,
    build_summary_prompt,
    build_weekly_prompt,
)

logger = logging.getLogger(__name__)


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


class CodexCliAgent:
    """Summarises papers through local `codex exec` and a persisted Codex login."""

    def __init__(
        self,
        config: dict[str, Any],
        settings: AppSettings,
        run_command: RunCommand = subprocess.run,
    ):
        self.config = config
        self.settings = settings
        self.codex_home = settings.codex_home
        self.codex_model = settings.codex_model
        self.timeout_seconds = settings.codex_timeout_seconds
        self.schema_path = Path(__file__).with_name("codex_summary_schema.json")
        self.run_command = run_command

    def identify_important_papers(self, papers: list[Paper]) -> str:
        """Summarise all papers in batches, then merge into a single post."""
        if not papers:
            raise ValueError("No papers provided to summarise")

        max_chars = 122_000 * 4
        batches = batch_papers(papers, max_chars)
        intermediate = []

        for i, batch in enumerate(batches, 1):
            logger.info("Processing Codex batch %d / %d", i, len(batches))
            batch_text = "\n".join(format_paper(paper) for paper in batch)
            prompt = self._build_prompt(build_summary_prompt(self.config), batch_text)
            intermediate.append(self._run_codex(prompt))

        if len(intermediate) == 1:
            return intermediate[0]

        combined_text = "\n\n".join(intermediate)
        prompt = self._build_prompt(build_combine_prompt(self.config), combined_text)
        return self._run_codex(prompt)

    def summarize_weekly(self, daily_reports: list[str]) -> str:
        if not daily_reports:
            raise ValueError("No daily reports provided")
        content = "\n\n--- DAILY REPORT ---\n\n".join(daily_reports)
        return self._run_codex(self._build_prompt(build_weekly_prompt(self.config), content))

    def _build_prompt(self, instructions: str, content: str) -> str:
        return (
            f"{instructions.rstrip()}\n\n"
            "Return the final answer as JSON matching the provided schema, with the "
            "complete Markdown blog summary in the `summary` field.\n\n"
            f"{content}"
        )

    def _run_codex(self, prompt: str) -> str:
        if self.codex_home is not None and not self.codex_home.is_dir():
            raise RuntimeError(
                f"CODEX_HOME does not exist: {self.codex_home}. "
                "Create it and run `codex login --device-auth` before using LLM_BACKEND=codex_cli."
            )

        with tempfile.TemporaryDirectory(prefix="paperpulse-codex-") as tmpdir:
            output_path = Path(tmpdir) / "summary.json"
            command = [
                "codex",
                "exec",
                "-",
                "--sandbox",
                "read-only",
                "--ephemeral",
                "--skip-git-repo-check",
                "--output-schema",
                str(self.schema_path),
                "--output-last-message",
                str(output_path),
            ]
            if self.codex_model:
                command.extend(["--model", self.codex_model])

            env = os.environ.copy()
            if self.codex_home is not None:
                env["CODEX_HOME"] = str(self.codex_home)

            try:
                completed = self.run_command(
                    command,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    env=env,
                    cwd=self.settings.project_dir,
                    shell=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    f"Codex CLI timed out after {self.timeout_seconds} seconds"
                ) from exc

            if completed.returncode != 0:
                stderr = (completed.stderr or "").strip()
                raise RuntimeError(
                    f"Codex CLI failed with exit code {completed.returncode}: {stderr}"
                )

            raw_output = self._read_codex_output(output_path, completed.stdout)
            return self._parse_summary(raw_output)

    def _read_codex_output(self, output_path: Path, stdout: str | None) -> str:
        if output_path.is_file():
            return output_path.read_text(encoding="utf-8")
        return stdout or ""

    def _parse_summary(self, raw_output: str) -> str:
        try:
            payload = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Codex CLI returned invalid JSON") from exc

        summary = payload.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise RuntimeError("Codex CLI returned an empty or missing summary")
        return summary
