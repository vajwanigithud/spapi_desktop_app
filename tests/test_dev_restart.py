import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

import main


def _build_dev_app() -> FastAPI:
    app = FastAPI()
    app.get("/api/dev/restart-enabled")(main.api_dev_restart_enabled)
    app.post("/api/dev/restart")(main.api_dev_restart)
    return app


def test_restart_endpoint_hidden_when_env_disabled(monkeypatch):
    monkeypatch.delenv("DEV_ALLOW_RESTART", raising=False)
    app = _build_dev_app()
    client = TestClient(app, base_url="http://127.0.0.1")

    resp_get = client.get("/api/dev/restart-enabled")
    resp_post = client.post("/api/dev/restart")

    assert resp_get.status_code in (403, 404)
    assert resp_post.status_code in (403, 404)


def test_restart_rejects_non_localhost(monkeypatch):
    monkeypatch.setenv("DEV_ALLOW_RESTART", "1")
    monkeypatch.setattr(main, "_get_client_host", lambda request: "10.0.0.5")
    app = _build_dev_app()
    client = TestClient(app)

    resp = client.post("/api/dev/restart")

    assert resp.status_code == 403


def test_restart_allows_localhost_and_schedules_exit(monkeypatch):
    monkeypatch.setenv("DEV_ALLOW_RESTART", "1")
    exit_called = threading.Event()

    def _fake_exit(code):
        exit_called.set()

    monkeypatch.setattr(main, "_DEV_RESTART_EXIT", _fake_exit)
    monkeypatch.setattr(main, "DEV_RESTART_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(main, "_get_client_host", lambda request: "127.0.0.1")
    app = _build_dev_app()
    client = TestClient(app)

    resp = client.post("/api/dev/restart")
    data = resp.json()

    assert resp.status_code == 200
    assert data.get("ok") is True
    assert data.get("restarting") is True
    assert exit_called.wait(timeout=1.0)
