"""Compatibility entrypoint for the multi-topic daily pipeline."""

from dataclasses import dataclass

from dotenv import load_dotenv

from api.pipeline import run_daily


@dataclass(frozen=True)
class PipelineResult:
    ok: bool
    message: str
    num_papers: int = 0
    post_path: str | None = None


def main() -> PipelineResult:
    load_dotenv()
    result = run_daily()
    return PipelineResult(
        ok=result.failed == 0,
        message=(
            f"Daily topic run complete: {result.succeeded} published, "
            f"{result.skipped} skipped, {result.failed} failed"
        ),
    )


if __name__ == "__main__":
    print(main().message)
