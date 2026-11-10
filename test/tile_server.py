"""Serve test/fixtures over HTTP with Range support, for the SQL tests.

Prints ``PORT:<n>`` on stdout once listening (ci/run-integration.sh waits for it).
"""

from __future__ import annotations

import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "fixtures"


class RangeHandler(SimpleHTTPRequestHandler):
    """Static files plus single ``bytes=a-b`` ranges (what PMTiles readers send)."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)  # type: ignore[arg-type]

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        if os.environ.get("TILE_SERVER_LOG"):
            sys.stderr.write(format % args + "\n")

    def do_GET(self) -> None:
        rng = self.headers.get("Range")
        path = Path(self.translate_path(self.path))
        if not rng or not path.is_file():
            super().do_GET()
            return
        data = path.read_bytes()
        start_s, _, end_s = rng.removeprefix("bytes=").partition("-")
        start = int(start_s)
        end = min(int(end_s) if end_s else len(data) - 1, len(data) - 1)
        if start >= len(data):
            self.send_response(416)
            self.end_headers()
            return
        body = data[start : end + 1]
        self.send_response(206)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 0), RangeHandler)
    print(f"PORT:{server.server_address[1]}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
