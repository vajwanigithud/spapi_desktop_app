import os
import sys
import time
import subprocess
import threading
import socket
import atexit
import tempfile

import pystray
from pystray import MenuItem as Item
from PIL import Image, ImageDraw


APP_ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHONW = os.path.join(APP_ROOT, ".venv", "Scripts", "pythonw.exe")
VENV_PYTHON  = os.path.join(APP_ROOT, ".venv", "Scripts", "python.exe")  # fallback
PORT = 8001
HOST = "127.0.0.1"

LOCK_PATH = os.path.join(tempfile.gettempdir(), "spapi_desktop_app_tray.lock")
PID_PATH = os.path.join(tempfile.gettempdir(), "spapi_desktop_app_uvicorn.pid")


def _port_is_listening(host=HOST, port=PORT, timeout=0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _kill_listeners_on_port(port=PORT):
    # Kills only the process(es) LISTENING on that port.
    # Uses built-in netstat + taskkill (no extra deps).
    cmd = (
        'cmd /c for /f "tokens=5" %a in (\'netstat -ano ^| findstr :%d ^| findstr LISTENING\') do taskkill /F /PID %a'
        % port
    )
    subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _open_browser():
    import webbrowser
    webbrowser.open(f"http://{HOST}:{PORT}/")


def _make_icon_image():
    # Simple green circle with "SP"
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill=(22, 163, 74, 255))  # green
    d.text((18, 20), "SP", fill=(255, 255, 255, 255))
    return img


class TrayApp:
    def __init__(self):
        self.icon = pystray.Icon("SP-API Desktop App")
        self.icon.icon = _make_icon_image()
        self.icon.title = "SP-API Desktop App"
        self._server_lock = threading.Lock()

        self.icon.menu = pystray.Menu(
            Item("Open UI (http://127.0.0.1:8001)", self.open_ui),
            Item("Start server", self.start_server, enabled=lambda item: not _port_is_listening()),
            Item("Stop server", self.stop_server, enabled=lambda item: _port_is_listening()),
            pystray.Menu.SEPARATOR,
            Item("Exit", self.exit_app),
        )

    def _notify(self, msg: str):
        try:
            self.icon.notify(msg, "SP-API Desktop App")
        except Exception:
            pass

    def open_ui(self, icon=None, item=None):
        _open_browser()

    def start_server(self, icon=None, item=None):
        with self._server_lock:
            if _port_is_listening():
                self._notify("Already running on port 8001.")
                return

            py = VENV_PYTHONW if os.path.exists(VENV_PYTHONW) else VENV_PYTHON
            if not os.path.exists(py):
                self._notify("Venv python not found. Check .venv\\Scripts.")
                return

            # Start uvicorn without --reload (reload spawns extra processes & causes confusion)
            args = [
                py, "-m", "uvicorn", "main:app",
                "--host", HOST,
                "--port", str(PORT),
                "--log-level", "info",
            ]

            p = subprocess.Popen(
                args,
                cwd=APP_ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

            # Store PID (best effort)
            try:
                with open(PID_PATH, "w", encoding="utf-8") as f:
                    f.write(str(p.pid))
            except Exception:
                pass

            # Wait a moment and notify
            time.sleep(0.4)
            if _port_is_listening():
                self._notify("Started on port 8001.")
            else:
                self._notify("Tried to start, but port 8001 is still not listening.")

    def stop_server(self, icon=None, item=None):
        with self._server_lock:
            if not _port_is_listening():
                self._notify("Not running.")
                return

            # Try targeted kill by port (most reliable)
            _kill_listeners_on_port(PORT)
            time.sleep(0.3)

            if not _port_is_listening():
                self._notify("Stopped.")
            else:
                self._notify("Stop attempted, but port 8001 is still listening.")

    def exit_app(self, icon=None, item=None):
        # Don’t auto-stop on exit (safer). If you want it, tell me.
        self.icon.stop()

    def run(self):
        self.icon.run()


def _acquire_single_instance_lock():
    # Create a lock file exclusively. If it exists, we’re already running.
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        # If lock fails oddly, still try to run (but likely fine)
        return True


def _release_lock():
    try:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
    except Exception:
        pass


def main():
    if not _acquire_single_instance_lock():
        # Already running -> exit silently
        return

    atexit.register(_release_lock)

    app = TrayApp()
    app.run()


if __name__ == "__main__":
    main()
