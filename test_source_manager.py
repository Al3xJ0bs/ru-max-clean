#!/usr/bin/env python3
from __future__ import annotations

import email.utils
import http.server
import json
import os
import socketserver
import tempfile
import threading
import time
from pathlib import Path

from source_manager import SourceCache, USER_AGENT, _download_parallel_ranges

STATE = {
    "body": b"version-one",
    "etag": '"v1"',
    "last_modified": "Mon, 17 Aug 2026 10:00:00 GMT",
    "get_count": 0,
    "head_count": 0,
}

RANGE_BODY = b"range-download-fixture-" * 8192
RANGE_STATE = {"requests": 0, "rate_limited": set()}


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


class Range429Handler(http.server.BaseHTTPRequestHandler):
    """Return one deterministic 429 per range, then serve the requested bytes."""

    def log_message(self, *_args):
        pass

    def do_GET(self):
        value = self.headers.get("Range", "")
        if not value.startswith("bytes=") or "-" not in value:
            self.send_response(416)
            self.end_headers()
            return
        start_text, end_text = value.removeprefix("bytes=").split("-", 1)
        start, end = int(start_text), int(end_text)
        key = (start, end)
        RANGE_STATE["requests"] += 1
        if key not in RANGE_STATE["rate_limited"]:
            RANGE_STATE["rate_limited"].add(key)
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.end_headers()
            return
        body = RANGE_BODY[start:end + 1]
        self.send_response(206)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(RANGE_BODY)}")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(RANGE_BODY)))
        self.end_headers()


def main():
    assert USER_AGENT.startswith("Mozilla/5.0 ")
    assert "RU-Max-Clean/4.9.5" in USER_AGENT
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

    # A range burst that receives HTTP 429 must be retried as smaller waves,
    # not discarded in favour of restarting the entire multi-hundred-MiB stream.
    RANGE_STATE["requests"] = 0
    RANGE_STATE["rate_limited"] = set()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        dest = root / "range.bin"
        old_retries = os.environ.get("RU_MAX_DOWNLOAD_RETRIES")
        os.environ["RU_MAX_DOWNLOAD_RETRIES"] = "0"
        try:
            with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Range429Handler) as srv:
                thread = threading.Thread(target=srv.serve_forever, daemon=True)
                thread.start()
                url = f"http://127.0.0.1:{srv.server_address[1]}/range.bin"
                meta = _download_parallel_ranges(url, dest, "range fixture", len(RANGE_BODY), 4, timeout=10)
                assert dest.read_bytes() == RANGE_BODY
                assert meta["rate_limit_waves"] >= 1
                srv.shutdown(); thread.join(timeout=2)
        finally:
            if old_retries is None:
                os.environ.pop("RU_MAX_DOWNLOAD_RETRIES", None)
            else:
                os.environ["RU_MAX_DOWNLOAD_RETRIES"] = old_retries

    print("SOURCE UPDATE TESTS PASSED")


if __name__ == "__main__":
    main()
