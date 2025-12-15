import os
import subprocess
import sys
import time

import webview

API_HOST = "127.0.0.1"
API_PORT = 8001


def run_api():
    env = os.environ.copy()
    # ensure correct venv python for uvicorn
    python_exe = os.path.join(os.path.dirname(sys.executable), "python.exe")
    return subprocess.Popen([
        python_exe,
        "-m",
        "uvicorn",
        "main:app",
        "--host",
        API_HOST,
        "--port",
        str(API_PORT),
    ], env=env)


def open_window():
    # small delay so uvicorn can start
    time.sleep(1.5)
    webview.create_window(
        "SP-API Desktop",
        f"http://{API_HOST}:{API_PORT}/ui/index.html",
        width=1280,
        height=800,
    )
    webview.start()


def main():
    proc = run_api()
    try:
        open_window()
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()

