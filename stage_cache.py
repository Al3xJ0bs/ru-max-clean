#!/usr/bin/env python3
"""Persistent pipeline-stage snapshots for RU Max Clean.

The important distinction from the source download cache is that these files contain
already parsed SQLite state. A later builder revision can therefore reuse expensive
lexical / Wikipedia stages when the rules for those stages did not change.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Iterable


def _sample_hash(path: Path, block: int = 128 * 1024) -> str:
    """Cheap corruption/change fingerprint without hashing a 6-GB dump every run."""
    h = hashlib.blake2b(digest_size=16)
    st = path.stat()
    h.update(str(st.st_size).encode())
    with path.open("rb") as f:
        head = f.read(block)
        h.update(head)
        if st.st_size > block:
            try:
                f.seek(max(0, st.st_size - block))
                h.update(f.read(block))
            except OSError:
                pass
    return h.hexdigest()


def file_fingerprint(path: Path | None) -> dict[str, object] | None:
    if path is None or not path.exists():
        return None
    st = path.stat()
    return {
        "name": path.name,
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
        "sample": _sample_hash(path),
    }


def files_fingerprint(paths: Iterable[Path]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for path in sorted((Path(p) for p in paths if Path(p).exists()), key=lambda p: str(p).casefold()):
        st = path.stat()
        h = hashlib.blake2b(digest_size=16)
        h.update(path.read_bytes())
        out.append({"path": str(path), "size": int(st.st_size), "hash": h.hexdigest()})
    return out


def signature(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class StageCache:
    def __init__(self, cache_dir: Path) -> None:
        self.root = Path(cache_dir) / "stage-cache"
        self.root.mkdir(parents=True, exist_ok=True)

    def _db(self, name: str) -> Path:
        return self.root / f"{name}.sqlite3"

    def _meta(self, name: str) -> Path:
        return self.root / f"{name}.json"

    def load_meta(self, name: str) -> dict[str, object]:
        try:
            raw = json.loads(self._meta(name).read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def valid(self, name: str, sig: str) -> bool:
        db = self._db(name)
        meta = self.load_meta(name)
        if not db.exists() or not meta:
            return False
        try:
            return (
                meta.get("signature") == sig
                and int(meta.get("db_size", -1)) == db.stat().st_size
                and db.stat().st_size > 0
            )
        except Exception:
            return False

    def restore(self, name: str, destination: Path) -> dict[str, object]:
        src = self._db(name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(destination) + ".restore.tmp")
        tmp.unlink(missing_ok=True)
        shutil.copyfile(src, tmp)
        tmp.replace(destination)
        return self.load_meta(name)

    def save(
        self,
        name: str,
        source_db: Path,
        sig: str,
        *,
        stats: dict[str, object] | None = None,
        source_conn: sqlite3.Connection | None = None,
    ) -> dict[str, object]:
        """Save a consistent SQLite stage snapshot.

        When the build database is still open, Windows can deny ordinary file reads
        while SQLite holds an EXCLUSIVE lock.  In that case a raw shutil.copyfile()
        is both fragile and, with journaling modes other than OFF, potentially
        inconsistent.  SQLite's online backup API reads through the owning
        connection and produces a transactionally consistent destination without
        releasing the live build database.

        A plain file copy remains available for callers that only have a closed
        database path (mainly tests/tools).
        """
        dst = self._db(name)
        tmp = Path(str(dst) + ".tmp")
        tmp.unlink(missing_ok=True)

        if source_conn is not None:
            # Do not VACUUM/close/reopen the live database: the caller may continue
            # using it immediately after the snapshot.  backup() is safe with the
            # source connection open and is the supported way to snapshot a locked
            # SQLite database on Windows.
            backup_conn = sqlite3.connect(tmp)
            try:
                backup_conn.execute("PRAGMA journal_mode=OFF")
                backup_conn.execute("PRAGMA synchronous=OFF")
                source_conn.backup(backup_conn, pages=32768, sleep=0.001)
                backup_conn.commit()
            finally:
                backup_conn.close()
        else:
            shutil.copyfile(source_db, tmp)

        tmp.replace(dst)
        meta: dict[str, object] = {
            "name": name,
            "signature": sig,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "db_size": dst.stat().st_size,
            "stats": stats or {},
        }
        mtmp = Path(str(self._meta(name)) + ".tmp")
        mtmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        mtmp.replace(self._meta(name))
        return meta

    def describe(self, name: str) -> str:
        db = self._db(name)
        if not db.exists():
            return "нет"
        return f"{db.stat().st_size / (1024**2):.1f} MiB"


class ArtifactCache:
    """Cache final StarDict artifacts separately from parsed SQLite stages.

    A parsed form-stage DB can still take tens of seconds to export because the
    StarDict index contains millions of keys.  This cache reuses already exported
    .ifo/.idx/.dict files whenever the semantic/form signature and export rules are
    unchanged.  Files are copied, never hard-linked, so a later forced rebuild
    cannot accidentally truncate the cached artifact through a shared inode.
    """

    def __init__(self, cache_dir: Path) -> None:
        self.root = Path(cache_dir) / "artifact-cache"
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, name: str) -> Path:
        return self.root / name

    def _meta(self, name: str) -> Path:
        return self._dir(name) / "meta.json"

    def load_meta(self, name: str) -> dict[str, object]:
        try:
            raw = json.loads(self._meta(name).read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def valid(self, name: str, sig: str) -> bool:
        d = self._dir(name)
        meta = self.load_meta(name)
        if not meta or meta.get("signature") != sig:
            return False
        files = meta.get("files")
        if not isinstance(files, dict):
            return False
        try:
            for filename, expected_size in files.items():
                p = d / str(filename)
                if not p.exists() or p.stat().st_size != int(expected_size):
                    return False
            return True
        except Exception:
            return False

    def save(
        self, name: str, sig: str, base: Path, *,
        stats: dict[str, object] | None = None,
        extra_files: Iterable[Path] = (),
    ) -> dict[str, object]:
        d = self._dir(name)
        tmp = self.root / f".{name}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        files: dict[str, int] = {}
        required = [base.with_suffix("." + ext) for ext in ("ifo", "idx", "dict")]
        optional = [Path(x) for x in extra_files if Path(x).exists()]
        for src in required + optional:
            if not src.exists():
                raise FileNotFoundError(src)
            dst = tmp / src.name
            shutil.copyfile(src, dst)
            files[src.name] = int(dst.stat().st_size)
        meta: dict[str, object] = {
            "name": name,
            "signature": sig,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "files": files,
            "stats": stats or {},
        }
        (tmp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        old = self.root / f".{name}.old"
        shutil.rmtree(old, ignore_errors=True)
        if d.exists():
            d.replace(old)
        tmp.replace(d)
        shutil.rmtree(old, ignore_errors=True)
        return meta

    def restore(self, name: str, sig: str, output_dir: Path) -> dict[str, object] | None:
        if not self.valid(name, sig):
            return None
        d = self._dir(name)
        output_dir.mkdir(parents=True, exist_ok=True)
        meta = self.load_meta(name)
        for filename in (meta.get("files") or {}):
            src = d / str(filename)
            dst = output_dir / str(filename)
            tmp = Path(str(dst) + ".restore.tmp")
            tmp.unlink(missing_ok=True)
            shutil.copyfile(src, tmp)
            tmp.replace(dst)
        stats = meta.get("stats")
        return stats if isinstance(stats, dict) else {}

    def restore_latest(self, name: str, output_dir: Path) -> dict[str, object] | None:
        """Restore a complete cached artifact when no source dump is available.

        Menu item 6 is explicitly a cache-only operation.  Requiring the original
        multi-gigabyte inputs merely to calculate a stage signature defeated that
        promise when a valid StarDict artifact was already present.  This method
        still checks every recorded file and its size before copying; normal builds
        continue to use the stricter signature-aware :meth:`restore` path.
        """
        d = self._dir(name)
        meta = self.load_meta(name)
        files = meta.get("files") if isinstance(meta, dict) else None
        if not d.exists() or not isinstance(files, dict) or not files:
            return None
        try:
            for filename, expected_size in files.items():
                path = d / str(filename)
                if not path.exists() or path.stat().st_size != int(expected_size):
                    return None
        except (OSError, TypeError, ValueError):
            return None
        output_dir.mkdir(parents=True, exist_ok=True)
        for filename in files:
            src = d / str(filename)
            dst = output_dir / str(filename)
            tmp = Path(str(dst) + ".restore.tmp")
            tmp.unlink(missing_ok=True)
            shutil.copyfile(src, tmp)
            tmp.replace(dst)
        stats = meta.get("stats")
        return stats if isinstance(stats, dict) else {}

    def describe(self, name: str) -> str:
        d = self._dir(name)
        if not d.exists():
            return "нет"
        total = sum(p.stat().st_size for p in d.iterdir() if p.is_file() and p.name != "meta.json")
        return f"{total / (1024**2):.1f} MiB"
