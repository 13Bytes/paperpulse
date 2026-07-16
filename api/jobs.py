"""Command-line entrypoint used by the scheduler."""

import argparse

from dotenv import load_dotenv

from api.auth import cleanup_expired_auth
from api.database import SessionLocal, assert_schema_current
from api.pipeline import run_daily, run_weekly


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job", choices=["daily", "weekly", "cleanup"])
    args = parser.parse_args()
    load_dotenv()
    assert_schema_current()
    if args.job == "cleanup":
        with SessionLocal() as db:
            links, sessions = cleanup_expired_auth(db)
            db.commit()
        print(f"cleanup: {links} magic links, {sessions} sessions deleted")
        return 0
    result = run_daily() if args.job == "daily" else run_weekly()
    print(
        f"{args.job}: {result.succeeded} succeeded, "
        f"{result.skipped} skipped, {result.failed} failed"
    )
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
