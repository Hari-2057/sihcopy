"""Lightweight operational server for INCOIS & MoES OceanEmbed Platform.

Serves the web/ directory on http://localhost:8080.
"""

from __future__ import annotations

import http.server
import socketserver
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
PORT = 8080


class CustomHTTPHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def end_headers(self):
        # Enable CORS and caching headers
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()


def run_server():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), CustomHTTPHandler) as httpd:
        print(f"\n=======================================================")
        print(f"  OCEANEMBED PLATFORM (INCOIS & MoES)")
        print(f"  Server running at: http://localhost:{PORT}")
        print(f"  Serving directory: {WEB_DIR}")
        print(f"=======================================================\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server...")
            httpd.server_close()


if __name__ == "__main__":
    run_server()
