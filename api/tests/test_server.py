import importlib

import pytest

pytest.importorskip("fastapi")


def load_server(monkeypatch, manual_triggers_allowed: bool):
    monkeypatch.setenv("MANUAL_TRIGGERS_ALLOWED", str(manual_triggers_allowed).lower())
    import api.server as server

    return importlib.reload(server)


def route_paths(app):
    return {route.path for route in app.routes}


def endpoint_for(app, path):
    for route in app.routes:
        if route.path == path:
            return route.endpoint
    raise AssertionError(f"Route not found: {path}")


def test_health_endpoint(monkeypatch):
    server = load_server(monkeypatch, manual_triggers_allowed=False)

    assert "/health" in route_paths(server.app)
    assert endpoint_for(server.app, "/health")() == {"status": "ok"}


def test_manual_trigger_routes_are_absent_by_default(monkeypatch):
    server = load_server(monkeypatch, manual_triggers_allowed=False)

    assert "/trigger" not in route_paths(server.app)
    assert "/trigger/status" not in route_paths(server.app)


def test_manual_trigger_routes_exist_when_enabled(monkeypatch):
    server = load_server(monkeypatch, manual_triggers_allowed=True)

    assert "/trigger" in route_paths(server.app)
    assert "/trigger/status" in route_paths(server.app)
    assert endpoint_for(server.app, "/trigger/status")() == {
        "running": False,
        "last_result": None,
        "message": None,
        "error": None,
    }


def test_trigger_marks_run_active_before_thread_starts(monkeypatch):
    server = load_server(monkeypatch, manual_triggers_allowed=True)

    class FakeThread:
        def __init__(self, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self):
            pass

    monkeypatch.setattr(server.threading, "Thread", FakeThread)

    response = endpoint_for(server.app, "/trigger")()

    assert response == {"status": "started", "message": "Pipeline started"}
    assert endpoint_for(server.app, "/trigger/status")() == {
        "running": True,
        "last_result": None,
        "message": "Pipeline queued",
        "error": None,
    }


def test_run_pipeline_records_failure_message(monkeypatch):
    server = load_server(monkeypatch, manual_triggers_allowed=True)
    import api.main as main_module

    monkeypatch.setattr(
        main_module,
        "main",
        lambda: main_module.PipelineResult(ok=False, message="Codex login required"),
    )
    with server._run_lock:
        server._run_state["running"] = True

    server._run_pipeline()

    assert endpoint_for(server.app, "/trigger/status")() == {
        "running": False,
        "last_result": "error",
        "message": "Codex login required",
        "error": "Codex login required",
    }


def test_safe_message_redacts_api_keys(monkeypatch):
    server = load_server(monkeypatch, manual_triggers_allowed=True)

    assert server._safe_message("bad key sk-proj-secret123") == "bad key [redacted-api-key]"
