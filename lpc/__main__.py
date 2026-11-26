"""Start the console.

    python -m lpc                 # desktop window (needs the 'desktop' extra: pywebview)
    python -m lpc --server-only   # just the local web server; open the printed URL in a browser

The server binds to 127.0.0.1 only, on a free port, so nothing is reachable from the network.
"""

from __future__ import annotations

import argparse
import socket
import threading
import time

import httpx
import uvicorn


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_up(url: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url + "/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError("server did not start")


def main() -> None:
    parser = argparse.ArgumentParser(prog="lpc", description="Law Practice Console")
    parser.add_argument("--server-only", action="store_true", help="run the web server without a window")
    parser.add_argument("--port", type=int, default=0, help="port (default: any free port)")
    args = parser.parse_args()

    from .app import create_app
    from .backup import start_daily_backups
    from .config import load_settings

    settings = load_settings()
    app = create_app(settings)          # applies any new migrations first
    start_daily_backups(settings.db_path, settings.backup_dir)

    port = args.port or _free_port()
    url = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    if args.server_only:
        print(f"Law Practice Console running at {url}  (Ctrl+C to stop)")
        server.run()
        return

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_until_up(url)

    import webview  # pywebview; on Windows it renders with Microsoft Edge WebView2

    webview.create_window("Law Practice Console", url, width=1400, height=900, min_size=(1000, 700))
    webview.start()
    server.should_exit = True
    thread.join(timeout=5)


if __name__ == "__main__":
    main()
