"""Command-line entrypoint used by the scheduler."""

import argparse

from dotenv import load_dotenv

from api.database import create_schema
from api.pipeline import run_daily, run_weekly


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job", choices=["daily", "weekly"])
    args = parser.parse_args()
    load_dotenv()
    create_schema()
    result = run_daily() if args.job == "daily" else run_weekly()
    print(
        f"{args.job}: {result.succeeded} succeeded, "
        f"{result.skipped} skipped, {result.failed} failed"
    )
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
