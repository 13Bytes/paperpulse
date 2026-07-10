from fastapi.testclient import TestClient

from api.server import app


def test_health_endpoint():
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}


def test_public_routes_are_registered():
    paths = {route.path for route in app.routes}
    assert {"/", "/topics", "/reports/{report_id}", "/login", "/account", "/admin"} <= paths
