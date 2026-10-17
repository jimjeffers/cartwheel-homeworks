"""Serve the session viewer and the current hw1-session.jsonl on localhost.

Run from the repository root: python3 -m analysis.view_sessions
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]


class ViewerHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        # Serve only the viewer and recording, never the rest of the repository.
        routes = {
            "/": (REPO_ROOT / "analysis/ui/chat-viewer.html", "text/html"),
            "/hw1-session.jsonl": (REPO_ROOT / "hw1-session.jsonl", "application/x-ndjson"),
        }
        resource = routes.get(urlsplit(self.path).path)
        if resource is None:
            self.send_error(404)
            return
        path, content_type = resource
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            self.send_error(404, "File not found")
            return
        except OSError:
            self.send_error(500, "Could not read file")
            return
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8021)
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), ViewerHandler) as server:
        print(f"Session viewer: http://127.0.0.1:{server.server_port}/", flush=True)
        print("Refresh the page to read the latest hw1-session.jsonl. Ctrl+C to stop.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
