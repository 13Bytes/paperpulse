"""
Lightweight FastAPI server for Paperpulse.

When MANUAL_TRIGGERS_ALLOWED=true the /trigger endpoint is registered at
startup and allows running the summariser pipeline on demand.
When the env var is absent or false the route is never registered – the
server returns 404 for any unknown path, so it cannot be reached even from
tools like Postman.

Run (dev):
    uvicorn api.server:app --host 0.0.0.0 --port 8000 --reload
"""

import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.settings import parse_bool

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

MANUAL_TRIGGERS_ALLOWED = parse_bool(os.getenv("MANUAL_TRIGGERS_ALLOWED"))

# --- Run-state tracking -------------------------------------------------------

_run_lock = threading.Lock()
_run_state: dict[str, Any] = {
    "running": False,
    "last_result": None,
    "message": None,
    "error": None,
}  # guarded by _run_lock


def _safe_message(message: object) -> str:
    """Return a concise message suitable for the dev UI."""
    text = str(message or "").strip() or "Pipeline failed without details."
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted-api-key]", text)
    text = re.sub(r"sk-proj-[A-Za-z0-9_-]+", "[redacted-api-key]", text)
    return text


def _run_pipeline() -> None:
    """Execute main() in a background thread and update shared state."""
    # Import here so the server module can be imported without loading the
    # entire pipeline at module level (keeps startup fast and avoids side-effects).
    from api.main import main  # noqa: PLC0415

    try:
        logger.info("Manual trigger: pipeline started")
        pipeline_result = main()
        if pipeline_result.ok:
            result = "success"
            message = pipeline_result.message
            error = None
            logger.info("Manual trigger: pipeline finished: %s", message)
        else:
            result = "error"
            message = pipeline_result.message
            error = _safe_message(pipeline_result.message)
            logger.error("Manual trigger: pipeline failed: %s", error)
    except Exception as exc:  # noqa: BLE001
        result = "error"
        message = "Pipeline raised an unexpected exception"
        error = _safe_message(f"{type(exc).__name__}: {exc}")
        logger.exception("Manual trigger: pipeline raised an exception")
    finally:
        with _run_lock:
            _run_state["running"] = False
            _run_state["last_result"] = result
            _run_state["message"] = message
            _run_state["error"] = error


# --- App factory --------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN001
    if MANUAL_TRIGGERS_ALLOWED:
        logger.info("MANUAL_TRIGGERS_ALLOWED=true – /trigger endpoint is active")
    else:
        logger.info("MANUAL_TRIGGERS_ALLOWED not set – /trigger endpoint is disabled")
    yield


app = FastAPI(
    title="Paperpulse API",
    description="Internal API for the Paperpulse summariser",
    lifespan=lifespan,
)

# CORS: allow the Jekyll dev server (port 4000) to call the API (port 8000).
# In production this service is not exposed at all, so this is dev-safe.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4000", "http://127.0.0.1:4000"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# --- Always-on endpoints ------------------------------------------------------


@app.get("/health")
def health() -> dict:
    """Basic liveness probe."""
    return {"status": "ok"}


# --- Conditional endpoint – only wired up when the flag is set ----------------

if MANUAL_TRIGGERS_ALLOWED:

    @app.post("/trigger")
    def trigger_update() -> dict:
        """
        Start a pipeline run in the background.

        Returns immediately with {"status": "started"} or {"status": "already_running"}.
        Poll GET /trigger/status to check progress.
        """
        with _run_lock:
            if _run_state["running"]:
                return {
                    "status": "already_running",
                    "message": _run_state["message"] or "Pipeline is already running",
                }
            _run_state["running"] = True
            _run_state["last_result"] = None
            _run_state["message"] = "Pipeline queued"
            _run_state["error"] = None

        thread = threading.Thread(target=_run_pipeline, daemon=True)
        thread.start()
        return {"status": "started", "message": "Pipeline started"}

    @app.get("/trigger/status")
    def trigger_status() -> dict:
        """Return whether a pipeline run is currently in progress."""
        with _run_lock:
            return {
                "running": _run_state["running"],
                "last_result": _run_state["last_result"],
                "message": _run_state["message"],
                "error": _run_state["error"],
            }
