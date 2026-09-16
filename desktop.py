"""Normal desktop lifecycle: one mutex, retained Uvicorn server, one webview window."""
import atexit
import importlib
import logging
import os
import socket
import threading
import time
from pathlib import Path
from urllib.request import urlopen

from services.desktop_single_instance import SingleInstanceGuard

API_HOST, API_PORT = "127.0.0.1", 8001
URL = f"http://{API_HOST}:{API_PORT}/"
LOG = logging.getLogger("desktop_launcher")

def message_box(text, title="SP-API Desktop App"):
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)

def load_desktop_component():
    """Fail before app/server import so missing pywebview cannot start workers."""
    try:
        return importlib.import_module("webview")
    except ImportError:
        message_box("Desktop component pywebview is not installed. Run the application setup or reinstall requirements.", "Startup error")
        return None

def health_is_ok():
    try:
        with urlopen(f"{URL}api/ping", timeout=1) as response: return response.status == 200
    except Exception: return False

def port_is_in_use():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(.5)
        return sock.connect_ex((API_HOST, API_PORT)) == 0

def create_splash(on_close):
    try:
        import tkinter as tk
        splash = tk.Tk(); splash.overrideredirect(True); splash.attributes("-topmost", True)
        splash.protocol("WM_DELETE_WINDOW", on_close)
        tk.Label(splash, text="Starting Amazon SP-API Desktop App…", padx=28, pady=20).pack()
        splash.update_idletasks(); splash.update(); LOG.info("splash opened")
        return splash
    except Exception: return None

def close_splash(splash):
    if splash:
        try: splash.destroy(); LOG.info("splash closed")
        except Exception: pass

class DesktopRuntime:
    """Owns the server thread and makes repeated GUI close events harmless."""
    def __init__(self):
        self.server = None
        self.thread = None
        self._shutdown_lock = threading.Lock()
        self._shutdown_requested = False

    def start_server(self):
        import uvicorn
        self.server = uvicorn.Server(uvicorn.Config("main:app", host=API_HOST, port=API_PORT, log_level="info"))
        self.thread = threading.Thread(target=self.server.run, name="spapi-uvicorn", daemon=False)
        self.thread.start(); LOG.info("Uvicorn started")

    def request_shutdown(self):
        with self._shutdown_lock:
            if self._shutdown_requested: return False
            self._shutdown_requested = True
            LOG.info("worker shutdown requested; Uvicorn shutdown requested")
            if self.server: self.server.should_exit = True
            return True

    def shutdown(self, timeout=12):
        self.request_shutdown()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout)
            if self.thread.is_alive(): LOG.error("Uvicorn did not stop within %ss", timeout)
            else: LOG.info("Uvicorn stopped")

def wait_for_health(runtime, stop_event, timeout_seconds=30):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline and not stop_event.is_set():
        if health_is_ok(): return True
        if runtime.thread and not runtime.thread.is_alive(): return False
        time.sleep(.2)
    return False

def _configure_lifecycle_log():
    if LOG.handlers: return
    log_path = Path(__file__).with_name("logs") / "desktop_lifecycle.log"
    log_path.parent.mkdir(exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler); LOG.setLevel(logging.INFO); LOG.propagate = False

def main():
    _configure_lifecycle_log()
    guard = SingleInstanceGuard()
    if not guard.acquire():
        message_box("SP-API Desktop App is already running."); return 0
    LOG.info("mutex acquired")
    atexit.register(guard.release)
    runtime, cancelled, splash = DesktopRuntime(), threading.Event(), None
    try:
        webview = load_desktop_component()
        if webview is None:
            LOG.error("pywebview import preflight failed")
            return 1
        splash = create_splash(cancelled.set)
        if port_is_in_use():
            healthy = health_is_ok()
            message_box("SP-API Desktop App is already running." if healthy else "Port 8001 is being used by another application. SP-API Desktop App could not start.", "SP-API Desktop App" if healthy else "Startup error")
            return 0 if healthy else 1
        runtime.start_server()
        if not wait_for_health(runtime, cancelled):
            message_box("SP-API Desktop App could not become ready on port 8001. Check logs\\spapi_backend.log.", "Startup error")
            return 1
        close_splash(splash); splash = None
        window = webview.create_window("SP-API Desktop", URL, width=1280, height=800)
        def on_window_close(*_args):
            LOG.info("main window closing")
            runtime.request_shutdown()
        window.events.closing += on_window_close
        window.events.closed += on_window_close
        LOG.info("main window opened")
        webview.start()
        return 0
    finally:
        close_splash(splash)
        runtime.shutdown()
        guard.release(); LOG.info("mutex released; process exiting")

if __name__ == "__main__": raise SystemExit(main())
