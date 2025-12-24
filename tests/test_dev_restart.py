import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

import main


def _build_dev_app() -> FastAPI:
    app = FastAPI()
    app.get("/api/dev/restart-enabled")(main.api_dev_restart_enabled)
    app.post("/api/dev/restart-enabled")(main.api_dev_set_restart_enabled)
    app.post("/api/dev/restart")(main.api_dev_restart)
    return app


def test_restart_endpoints_present_and_disabled_by_default(monkeypatch):
    state = {"enabled": False}
    monkeypatch.setattr(main, "_read_dev_restart_flag", lambda: state["enabled"])
    monkeypatch.setattr(main, "_write_dev_restart_flag", lambda enabled: state.__setitem__("enabled", bool(enabled)))
    monkeypatch.setattr(main, "_get_client_host", lambda request: "127.0.0.1")

    app = _build_dev_app()
    client = TestClient(app)

    resp_get = client.get("/api/dev/restart-enabled")
    resp_restart = client.post("/api/dev/restart")

    assert resp_get.status_code == 200
    assert resp_get.json().get("enabled") is False
    assert resp_restart.status_code == 403
    assert resp_restart.json().get("detail") == "Dev restart disabled"


def test_restart_rejects_non_localhost(monkeypatch):
    state = {"enabled": True}
    monkeypatch.setattr(main, "_read_dev_restart_flag", lambda: state["enabled"])
    monkeypatch.setattr(main, "_write_dev_restart_flag", lambda enabled: state.__setitem__("enabled", bool(enabled)))
    monkeypatch.setattr(main, "_get_client_host", lambda request: "10.0.0.5")

    app = _build_dev_app()
    client = TestClient(app)

    resp_get = client.get("/api/dev/restart-enabled")
    resp_restart = client.post("/api/dev/restart")

    assert resp_get.status_code == 403
    assert resp_restart.status_code == 403


def test_restart_allows_localhost_after_toggle(monkeypatch):
    state = {"enabled": False}
    monkeypatch.setattr(main, "_read_dev_restart_flag", lambda: state["enabled"])
    monkeypatch.setattr(main, "_write_dev_restart_flag", lambda enabled: state.__setitem__("enabled", bool(enabled)))
    monkeypatch.setattr(main, "_get_client_host", lambda request: "127.0.0.1")
    exit_called = threading.Event()

    def _fake_exit(code):
        exit_called.set()

    monkeypatch.setattr(main, "_DEV_RESTART_EXIT", _fake_exit)
    monkeypatch.setattr(main, "DEV_RESTART_DELAY_SECONDS", 0.01)

    app = _build_dev_app()
    client = TestClient(app)

    toggle_resp = client.post("/api/dev/restart-enabled", json={"enabled": True})
    assert toggle_resp.status_code == 200
    assert toggle_resp.json().get("enabled") is True
    assert state["enabled"] is True

    restart_resp = client.post("/api/dev/restart")
    data = restart_resp.json()

    assert restart_resp.status_code == 200
    assert data.get("ok") is True
    assert data.get("restarting") is True
    assert exit_called.wait(timeout=1.0)
