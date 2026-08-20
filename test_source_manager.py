#!/usr/bin/env python3
from __future__ import annotations

import email.utils
import http.server
import json
import socketserver
import tempfile
import threading
import time
from pathlib import Path

from source_manager import SourceCache, USER_AGENT

STATE = {
    "body": b"version-one",
    "etag": '"v1"',
    "last_modified": "Mon, 17 Aug 2026 10:00:00 GMT",
    "get_count": 0,
    "head_count": 0,
}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _headers(self, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(STATE["body"])))
        self.send_header("ETag", STATE["etag"])
        self.send_header("Last-Modified", STATE["last_modified"])
        self.end_headers()

    def do_HEAD(self):
        STATE["head_count"] += 1
        self._headers()

    def do_GET(self):
        STATE["get_count"] += 1
        self._headers()
        self.wfile.write(STATE["body"])


def main():
    assert USER_AGENT.startswith("Mozilla/5.0 ")
    assert "RU-Max-Clean/4.9.4" in USER_AGENT
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv:
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{srv.server_address[1]}/source.bin"
            dest = root / "source.bin"

            cache = SourceCache(root)
            assert cache.ensure("fixture", url, dest, required=True) == dest
            assert dest.read_bytes() == b"version-one"
            first_gets = STATE["get_count"]
            assert first_gets == 1

            # Same ETag -> update check only; no redownload.
            cache = SourceCache(root)
            cache.ensure("fixture", url, dest, required=True)
            assert STATE["get_count"] == first_gets

            # Changed ETag/Last-Modified/body -> atomically replace the cache.
            STATE["body"] = b"version-two-is-newer"
            STATE["etag"] = '"v2"'
            STATE["last_modified"] = "Tue, 18 Aug 2026 10:00:00 GMT"
            cache = SourceCache(root)
            cache.ensure("fixture", url, dest, required=True)
            assert dest.read_bytes() == STATE["body"]
            assert STATE["get_count"] == first_gets + 1
            manifest = json.loads((root / "source_manifest.json").read_text(encoding="utf-8"))
            assert manifest["source.bin"]["etag"] == '"v2"'

            srv.shutdown()
            thread.join(timeout=2)

        # Network is gone; a cached source must still be usable rather than
        # destroying a working build because a source server is temporarily down.
        cache = SourceCache(root)
        assert cache.ensure("fixture", url, dest, required=True) == dest
        assert dest.read_bytes() == b"version-two-is-newer"

        # Explicit offline mode never tries the network and uses the same cache.
        cache = SourceCache(root, offline=True)
        assert cache.ensure("fixture", url, dest, required=True) == dest

    # Migration safety: an existing pre-4.x file without source_manifest.json must
    # not be trusted merely because its byte length matches the current remote
    # object. A one-time refresh establishes an ETag/Last-Modified fingerprint.
    STATE["body"] = b"same-size-new"
    STATE["etag"] = '"m2"'
    STATE["last_modified"] = "Tue, 18 Aug 2026 12:00:00 GMT"
    STATE["get_count"] = 0
    STATE["head_count"] = 0
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        dest = root / "source.bin"
        dest.write_bytes(b"old-size-data")  # same byte length, different content
        assert len(dest.read_bytes()) == len(STATE["body"])
        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv:
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{srv.server_address[1]}/source.bin"
            SourceCache(root).ensure("migration", url, dest, required=True)
            assert dest.read_bytes() == STATE["body"]
            assert STATE["get_count"] == 1
            assert (root / "source_manifest.json").exists()
            srv.shutdown(); thread.join(timeout=2)

    print("SOURCE UPDATE TESTS PASSED")


if __name__ == "__main__":
    main()

