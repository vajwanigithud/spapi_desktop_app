from unittest.mock import Mock
import sys
import types

import desktop


def test_second_launch_exits_before_port_or_server(monkeypatch):
    guard = Mock(); guard.acquire.return_value = False
    monkeypatch.setattr(desktop, "SingleInstanceGuard", lambda: guard)
    monkeypatch.setattr(desktop, "message_box", Mock())
    monkeypatch.setattr(desktop, "port_is_in_use", Mock(side_effect=AssertionError("must not probe")))
    assert desktop.main() == 0
    desktop.message_box.assert_called_once()


def test_missing_pywebview_exits_before_splash_or_server(monkeypatch):
    guard = Mock(); guard.acquire.return_value = True
    monkeypatch.setattr(desktop, "SingleInstanceGuard", lambda: guard)
    monkeypatch.setattr(desktop, "load_desktop_component", lambda: None)
    monkeypatch.setattr(desktop, "create_splash", Mock(side_effect=AssertionError("must not open")))
    monkeypatch.setattr(desktop.DesktopRuntime, "start_server", Mock(side_effect=AssertionError("must not start")))
    assert desktop.main() == 1
    assert guard.release.call_count == 1


def test_unrelated_port_owner_does_not_start_server(monkeypatch):
    guard = Mock(); guard.acquire.return_value = True
    monkeypatch.setattr(desktop, "SingleInstanceGuard", lambda: guard)
    monkeypatch.setattr(desktop, "create_splash", lambda _on_close: None)
    monkeypatch.setattr(desktop, "port_is_in_use", lambda: True)
    monkeypatch.setattr(desktop, "health_is_ok", lambda: False)
    monkeypatch.setattr(desktop, "message_box", Mock())
    monkeypatch.setattr(desktop.DesktopRuntime, "start_server", Mock(side_effect=AssertionError("must not start")))
    assert desktop.main() == 1
    assert "Port 8001" in desktop.message_box.call_args.args[0]


def test_healthy_fixed_port_is_reused_without_new_server(monkeypatch):
    guard = Mock(); guard.acquire.return_value = True
    monkeypatch.setattr(desktop, "SingleInstanceGuard", lambda: guard)
    monkeypatch.setattr(desktop, "create_splash", lambda _on_close: None)
    monkeypatch.setattr(desktop, "port_is_in_use", lambda: True)
    monkeypatch.setattr(desktop, "health_is_ok", lambda: True)
    monkeypatch.setattr(desktop, "message_box", Mock())
    monkeypatch.setattr(desktop.DesktopRuntime, "start_server", Mock(side_effect=AssertionError("must not start")))
    assert desktop.main() == 0
    assert "already running" in desktop.message_box.call_args.args[0].lower()


def test_guard_release_allows_next_launch(monkeypatch):
    from services.desktop_single_instance import SingleInstanceGuard
    guard = SingleInstanceGuard()
    monkeypatch.setattr("services.desktop_single_instance.os.name", "posix")
    assert guard.acquire() is True
    guard.release()
    assert guard.acquire() is True


def test_shutdown_callback_is_idempotent_and_stops_server():
    runtime = desktop.DesktopRuntime()
    server = Mock(); runtime.server = server
    assert runtime.request_shutdown() is True
    assert runtime.request_shutdown() is False
    assert server.should_exit is True


def test_shutdown_waits_for_server_thread():
    runtime = desktop.DesktopRuntime()
    thread = Mock(); thread.is_alive.side_effect = [True, False]
    runtime.thread = thread
    runtime.shutdown(timeout=1)
    thread.join.assert_called_once_with(1)


def test_window_close_requests_shutdown_once_and_releases_guard(monkeypatch):
    class Event:
        def __init__(self): self.callbacks = []
        def __iadd__(self, callback): self.callbacks.append(callback); return self
    window = types.SimpleNamespace(events=types.SimpleNamespace(closing=Event(), closed=Event()))
    webview = types.SimpleNamespace(create_window=lambda *args, **kwargs: window)
    def start():
        for callback in window.events.closing.callbacks + window.events.closed.callbacks: callback()
    webview.start = start
    guard = Mock(); guard.acquire.return_value = True
    monkeypatch.setattr(desktop, "SingleInstanceGuard", lambda: guard)
    monkeypatch.setattr(desktop, "create_splash", lambda _on_close: None)
    monkeypatch.setattr(desktop, "port_is_in_use", lambda: False)
    monkeypatch.setattr(desktop, "wait_for_health", lambda *_args: True)
    monkeypatch.setattr(desktop.DesktopRuntime, "start_server", lambda self: setattr(self, "server", Mock()))
    monkeypatch.setitem(sys.modules, "webview", webview)
    assert desktop.main() == 0
    # One outer cleanup sequence releases the process-owned mutex despite both GUI events.
    assert guard.release.call_count == 1
