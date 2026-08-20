#!/usr/bin/env python3
"""Persistent update-aware source cache for RU Max Clean.

Every online build probes the remote object first. Existing downloads are reused when
ETag/Last-Modified/Content-Length indicate that the object is unchanged. Changed
objects are downloaded to a .part file and atomically replaced. If a source is
temporarily unavailable, a previously cached copy is retained.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import json
import os
import sys
import threading
import concurrent.futures
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable

from progress_ui import render as progress_render, finish as progress_finish
from version_info import BUILDER_VERSION

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    f"RU-Max-Clean/{BUILDER_VERSION} (+KOReader offline dictionary builder)"
)
RETRYABLE_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})
DOWNLOAD_WORKERS_DEFAULT = 2
DOWNLOAD_WORKERS_MAX = 4
RANGE_RETRY_WAVES = 5
# After the burst has been reduced to one connection, allow a few serialized
# recovery waves with the normal Retry-After-aware request retry. Successful
# ranges stay on disk and are never downloaded again.
RANGE_SERIAL_RECOVERY_WAVES = RANGE_RETRY_WAVES + 3

# Wikimedia and Kaikki are happy with a sustained stream but may reject a burst
# of byte-range requests with HTTP 429.  Keep a tiny per-host request gate so a
# retry from one range cannot immediately stampede the same host from the other
# range workers.  The gate is process-local and deliberately does not serialize
# data reads: it only spaces request starts and honours a host-wide cooldown.
_HOST_GATE_LOCK = threading.Lock()
_HOST_NEXT_REQUEST: dict[str, float] = {}
_HOST_COOLDOWN_UNTIL: dict[str, float] = {}


def _host_key(req: urllib.request.Request) -> str:
    return urllib.parse.urlsplit(req.full_url).netloc.casefold()


def _request_interval() -> float:
    try:
        return max(0.0, min(5.0, float(os.environ.get("RU_MAX_DOWNLOAD_REQUEST_INTERVAL", "0.15"))))
    except ValueError:
        return 0.15


def _wait_for_request_slot(req: urllib.request.Request) -> None:
    host = _host_key(req)
    interval = _request_interval()
    with _HOST_GATE_LOCK:
        now = time.monotonic()
        target = max(now, _HOST_NEXT_REQUEST.get(host, 0.0), _HOST_COOLDOWN_UNTIL.get(host, 0.0))
        _HOST_NEXT_REQUEST[host] = target + interval
    delay = target - now
    if delay > 0:
        time.sleep(delay)


def _cooldown_host(req: urllib.request.Request, delay: float) -> None:
    host = _host_key(req)
    with _HOST_GATE_LOCK:
        _HOST_COOLDOWN_UNTIL[host] = max(_HOST_COOLDOWN_UNTIL.get(host, 0.0), time.monotonic() + max(0.0, delay))


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _headers_meta(headers, url: str) -> dict[str, object]:
    length = headers.get("Content-Length")
    try:
        length_i = int(length) if length not in (None, "") else None
    except ValueError:
        length_i = None
    return {
        "url": url,
        "etag": headers.get("ETag"),
        "last_modified": headers.get("Last-Modified"),
        "content_length": length_i,
    }


def _http_open(req: urllib.request.Request, timeout: int = 90):
    return urllib.request.urlopen(req, timeout=timeout)


def _retry_delay(exc: urllib.error.HTTPError, attempt: int) -> float:
    """Return a bounded backoff for a rate-limited/transient HTTP response."""
    retry_after = exc.headers.get("Retry-After") if exc.headers else None
    try:
        if retry_after:
            return min(30.0, max(0.25, float(retry_after)))
    except (TypeError, ValueError):
        try:
            retry_at = email.utils.parsedate_to_datetime(str(retry_after)).timestamp()
            return min(30.0, max(0.25, retry_at - time.time()))
        except (TypeError, ValueError, OverflowError):
            pass
    return min(30.0, 0.5 * (2 ** attempt))


def _open_with_retry(req: urllib.request.Request, timeout: int = 90, *, retry_rate_limit: bool = True):
    """Open a request with a small, deterministic retry budget.

    Wikimedia/Kaikki occasionally answer a burst of range requests with 429 or
    503.  Retrying those requests in-place keeps the download parallel without
    abandoning all pieces and dropping to a much slower single stream.
    """
    try:
        max_retries = max(0, min(5, int(os.environ.get("RU_MAX_DOWNLOAD_RETRIES", "4"))))
    except ValueError:
        max_retries = 4
    for attempt in range(max_retries + 1):
        try:
            _wait_for_request_slot(req)
            return _http_open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRYABLE_HTTP or attempt >= max_retries or (exc.code == 429 and not retry_rate_limit):
                raise
            delay = _retry_delay(exc, attempt)
            if exc.code == 429:
                _cooldown_host(req, delay)
            _log(f"[DOWNLOAD RETRY] HTTP {exc.code}; повтор через {delay:.1f} с")
            exc.close()
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt >= max_retries:
                raise
            delay = min(30.0, 0.5 * (2 ** attempt))
            _log(f"[DOWNLOAD RETRY] {exc}; повтор через {delay:.1f} с")
            time.sleep(delay)


def probe_remote(url: str, timeout: int = 45) -> dict[str, object]:
    """Return metadata without downloading the body when possible."""
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    req = urllib.request.Request(url, headers=headers, method="HEAD")
    try:
        with _open_with_retry(req, timeout=timeout) as response:
            meta = _headers_meta(response.headers, response.geturl())
            meta["status"] = getattr(response, "status", 200)
            return meta
    except urllib.error.HTTPError as exc:
        if exc.code not in (400, 403, 405, 501):
            raise
    # A few mirrors reject HEAD. A one-byte range request gives us headers without
    # pulling a multi-gigabyte dump. Servers that ignore Range are closed before the
    # response body is read.
    req = urllib.request.Request(
        url,
        headers={**headers, "Range": "bytes=0-0"},
        method="GET",
    )
    with _open_with_retry(req, timeout=timeout) as response:
        meta = _headers_meta(response.headers, response.geturl())
        content_range = response.headers.get("Content-Range")
        if content_range and "/" in content_range:
            tail = content_range.rsplit("/", 1)[-1]
            if tail.isdigit():
                meta["content_length"] = int(tail)
        meta["status"] = getattr(response, "status", 200)
        return meta


def _remote_equal(old: dict[str, object] | None, remote: dict[str, object], local_size: int) -> bool:
    """Conservative equality test for a cached object."""
    if old:
        old_etag, new_etag = old.get("etag"), remote.get("etag")
        if old_etag and new_etag:
            return old_etag == new_etag
        old_lm, new_lm = old.get("last_modified"), remote.get("last_modified")
        old_len, new_len = old.get("content_length"), remote.get("content_length")
        if old_lm and new_lm:
            if old_lm != new_lm:
                return False
            if old_len is not None and new_len is not None:
                return int(old_len) == int(new_len) == local_size
            return True
        if old_len is not None and new_len is not None:
            # Length alone is weaker than ETag/Last-Modified. It is acceptable only
            # when this same URL was already recorded in the manifest.
            return int(old_len) == int(new_len) == local_size and old.get("url") == remote.get("url")
    # Migration from pre-4.x builds: there is no trustworthy remote fingerprint
    # yet. Do NOT trust Content-Length alone; a changed rolling dump can, in
    # principle, have the same compressed size. Only seed the manifest without a
    # download when the local file timestamp already matches the server's
    # Last-Modified value. Otherwise perform one safe refresh and record ETag /
    # Last-Modified for all subsequent runs.
    return False


def _download_parallel_ranges(url: str, destination: Path, label: str, total: int, workers: int, timeout: int = 120) -> dict[str, object]:
    """Download one large immutable object with HTTP byte ranges.

    Falls back to the ordinary single-stream downloader if the server ignores
    Range.  This is deliberately limited to large files so small sources do not
    pay thread/setup overhead.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(str(destination) + ".part")
    temp.unlink(missing_ok=True)
    # Two connections are the safe default for Wikimedia/Kaikki.  Users may
    # explicitly lower/raise this through RU_MAX_DOWNLOAD_WORKERS, but never
    # above a safe hard cap.  If a host still answers 429, the wave scheduler
    # below backs off to one connection and retries only failed ranges.
    workers = max(2, min(int(workers), DOWNLOAD_WORKERS_MAX))
    chunk_size = (total + workers - 1) // workers
    pieces: list[tuple[int, int, Path]] = []
    for i in range(workers):
        start = i * chunk_size
        if start >= total:
            break
        end = min(total - 1, start + chunk_size - 1)
        pieces.append((start, end, Path(f"{temp}.{i:02d}")))
    progress_lock = threading.Lock()
    done = 0
    piece_done: dict[Path, int] = {part: 0 for _start, _end, part in pieces}
    meta_holder: dict[str, object] = {}

    def fetch(piece: tuple[int, int, Path]) -> None:
        nonlocal done
        start, end, part = piece
        # A failed range may have written a partial file before the server
        # returned 429.  Remove its contribution from the aggregate progress
        # before retrying so the displayed percentage never jumps past 100%.
        with progress_lock:
            done -= piece_done.get(part, 0)
            piece_done[part] = 0
            progress_render(label, max(0, done), total, unit="bytes")
        part.unlink(missing_ok=True)
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Encoding": "identity",
                "Range": f"bytes={start}-{end}",
            },
            method="GET",
        )
        # Let the range-wave scheduler see 429 immediately.  Retrying it inside
        # every range worker would keep the original burst alive for several
        # seconds and defeat adaptive downshifting.
        # During the initial waves, let the scheduler observe 429 immediately
        # and reduce concurrency.  In serialized recovery mode the normal
        # Retry-After-aware retry is preferable and still affects only this
        # failed range.
        with _open_with_retry(
            req,
            timeout=timeout,
            retry_rate_limit=wave > RANGE_RETRY_WAVES,
        ) as response, part.open("wb") as out:
            if getattr(response, "status", 200) != 206 or not response.headers.get("Content-Range"):
                raise OSError("server does not support HTTP range downloads")
            content_range = str(response.headers.get("Content-Range") or "")
            expected_range = f"bytes {start}-{end}/{total}"
            if not content_range.startswith(f"bytes {start}-{end}/"):
                raise OSError(
                    f"server returned wrong Content-Range {content_range!r}; expected {expected_range}"
                )
            with progress_lock:
                if not meta_holder:
                    meta_holder.update(_headers_meta(response.headers, response.geturl()))
                    meta_holder["content_length"] = total
            local_done = 0
            while True:
                block = response.read(2 * 1024 * 1024)
                if not block:
                    break
                out.write(block)
                local_done += len(block)
                with progress_lock:
                    delta = local_done - piece_done.get(part, 0)
                    done += delta
                    piece_done[part] = local_done
                    progress_render(label, done, total, unit="bytes")
            expected = end - start + 1
            if local_done != expected:
                raise OSError(f"incomplete range {start}-{end}: expected {expected}, got {local_done}")

    try:
        pending = list(pieces)
        active_workers = workers
        wave = 0
        while pending:
            failures: list[tuple[tuple[int, int, Path], Exception]] = []
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(active_workers, len(pending)),
                thread_name_prefix="ru-max-dl",
            ) as pool:
                future_map = {pool.submit(fetch, piece): piece for piece in pending}
                for fut in concurrent.futures.as_completed(future_map):
                    piece = future_map[fut]
                    try:
                        fut.result()
                    except Exception as exc:
                        failures.append((piece, exc))
            if not failures:
                break

            rate_limited = [exc for _piece, exc in failures if isinstance(exc, urllib.error.HTTPError) and exc.code == 429]
            if len(rate_limited) != len(failures):
                raise failures[0][1]
            wave += 1
            if wave > RANGE_SERIAL_RECOVERY_WAVES:
                raise failures[0][1]
            active_workers = max(1, active_workers // 2)
            delay = max(_retry_delay(exc, wave) for exc in rate_limited)
            _log(
                f"[DOWNLOAD THROTTLE] {label}: HTTP 429; повтор диапазонов "
                f"через {delay:.1f} с, соединений: {active_workers}"
            )
            time.sleep(delay)
            pending = [piece for piece, _exc in failures]
        digest = hashlib.sha256()
        with temp.open("wb") as out:
            for _start, _end, part in pieces:
                with part.open("rb") as inp:
                    while True:
                        block = inp.read(8 * 1024 * 1024)
                        if not block:
                            break
                        out.write(block)
                        digest.update(block)
        if temp.stat().st_size != total:
            raise OSError(f"parallel download size mismatch: expected {total}, got {temp.stat().st_size}")
        temp.replace(destination)
        meta = dict(meta_holder)
        meta.update({
            "url": str(meta.get("url") or url),
            "content_length": total,
            "sha256": digest.hexdigest(),
            "downloaded_at": _now(),
            "local_size": destination.stat().st_size,
            "download_mode": f"parallel-ranges:{len(pieces)}",
            "download_workers": active_workers,
            "rate_limit_waves": wave,
        })
        lm = meta.get("last_modified")
        if isinstance(lm, str) and lm:
            try:
                stamp = email.utils.parsedate_to_datetime(lm).timestamp()
                os.utime(destination, (stamp, stamp))
            except Exception:
                pass
        progress_finish(label, done, total, unit="bytes")
        return meta
    finally:
        for _start, _end, part in pieces:
            part.unlink(missing_ok=True)
        if temp.exists() and not destination.exists():
            temp.unlink(missing_ok=True)


def _download(url: str, destination: Path, label: str, timeout: int = 120, *, total_hint: int = 0, workers: int = 1) -> dict[str, object]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if workers > 1 and total_hint >= 128 * 1024 * 1024:
        try:
            return _download_parallel_ranges(url, destination, label, int(total_hint), workers, timeout=timeout)
        except Exception as exc:
            _log(f"[TURBO DOWNLOAD FALLBACK] {label}: {exc}; using one connection.")
    temp = Path(str(destination) + ".part")
    temp.unlink(missing_ok=True)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
        method="GET",
    )
    digest = hashlib.sha256()
    try:
        with _open_with_retry(req, timeout=timeout) as response, temp.open("wb") as out:
            meta = _headers_meta(response.headers, response.geturl())
            total = meta.get("content_length") or 0
            done = 0
            while True:
                chunk = response.read(4 * 1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                progress_render(label, done, int(total) if total else None, unit="bytes")
        progress_finish(label, done, int(total) if total else done, unit="bytes")
        expected = meta.get("content_length")
        if expected is not None and temp.stat().st_size != int(expected):
            raise OSError(
                f"incomplete download: expected {int(expected):,} bytes, got {temp.stat().st_size:,}"
            )
        temp.replace(destination)
        # Preserve the server timestamp when available. This is useful for humans
        # inspecting sources/ and provides an extra fallback update signal.
        lm = meta.get("last_modified")
        if isinstance(lm, str) and lm:
            try:
                stamp = email.utils.parsedate_to_datetime(lm).timestamp()
                os.utime(destination, (stamp, stamp))
            except Exception:
                pass
        meta.update({
            "sha256": digest.hexdigest(),
            "downloaded_at": _now(),
            "local_size": destination.stat().st_size,
        })
        return meta
    except Exception:
        temp.unlink(missing_ok=True)
        raise


class SourceCache:
    def __init__(self, cache_dir: Path, *, offline: bool = False, force_refresh: bool = False) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.cache_dir / "source_manifest.json"
        self.offline = offline
        self.force_refresh = force_refresh
        try:
            self.download_workers = max(1, min(DOWNLOAD_WORKERS_MAX, int(os.environ.get("RU_MAX_DOWNLOAD_WORKERS", str(DOWNLOAD_WORKERS_DEFAULT)))))
        except ValueError:
            self.download_workers = DOWNLOAD_WORKERS_DEFAULT
        self._lock = threading.RLock()
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            self.manifest: dict[str, dict[str, object]] = data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            self.manifest = {}

    def save(self) -> None:
        with self._lock:
            temp = Path(str(self.manifest_path) + ".tmp")
            temp.write_text(json.dumps(self.manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            temp.replace(self.manifest_path)

    def ensure(
        self,
        label: str,
        urls: str | Iterable[str],
        destination: Path,
        *,
        required: bool,
    ) -> Path | None:
        urls = [urls] if isinstance(urls, str) else list(urls)
        destination = Path(destination)
        key = destination.name
        with self._lock:
            old = dict(self.manifest.get(key) or {}) or None

        if self.offline:
            if destination.exists():
                _log(f"[OFFLINE] {label}: using cached {destination.name} ({destination.stat().st_size:,} bytes)")
                return destination
            if required:
                raise OSError(f"{label}: source missing in offline mode: {destination}")
            _log(f"WARNING: {label}: source missing in offline mode; layer skipped.")
            return None

        errors: list[str] = []
        for url in urls:
            try:
                _log(f"[CHECK] {label}: {url}")
                remote = probe_remote(url)
                if destination.exists() and not self.force_refresh and _remote_equal(old, remote, destination.stat().st_size):
                    merged = dict(old or {})
                    merged.update(remote)
                    merged.update({
                        "checked_at": _now(),
                        "local_size": destination.stat().st_size,
                        "label": label,
                    })
                    self.manifest[key] = merged
                    self.save()
                    lm = remote.get("last_modified") or "unknown date"
                    _log(f"[UP TO DATE] {label}: {destination.name} ({lm})")
                    return destination

                if destination.exists():
                    _log(f"[UPDATE AVAILABLE] {label}: remote metadata changed; replacing cached file.")
                else:
                    _log(f"[DOWNLOAD] {label}: cache is missing.")
                meta = _download(url, destination, label, total_hint=int(remote.get("content_length") or 0), workers=self.download_workers)
                meta.update({"checked_at": _now(), "label": label})
                self.manifest[key] = meta
                self.save()
                _log(f"[READY] {label}: {destination.name} ({destination.stat().st_size:,} bytes)")
                return destination
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
                errors.append(f"{url}: {exc}")
                _log(f"WARNING: {label}: {exc}")

        if destination.exists():
            _log(f"WARNING: {label}: update check/download failed; using cached {destination.name}.")
            if old is None:
                self.manifest[key] = {
                    "label": label,
                    "url": urls[0] if urls else "",
                    "local_size": destination.stat().st_size,
                    "checked_at": _now(),
                    "update_check_error": " | ".join(errors),
                }
                self.save()
            return destination
        if required:
            raise OSError(f"{label}: no usable source. " + " | ".join(errors))
        _log(f"WARNING: {label}: no cached source; optional layer skipped.")
        return None
