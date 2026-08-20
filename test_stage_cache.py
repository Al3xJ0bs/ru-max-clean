#!/usr/bin/env python3
from __future__ import annotations
import sqlite3
import tempfile
from pathlib import Path

from stage_cache import StageCache, ArtifactCache, file_fingerprint, signature


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        sources = root / "sources"
        source = root / "source.bin"
        source.write_bytes(b"abc" * 100)
        payload = {"rules": "x1", "source": file_fingerprint(source)}
        sig = signature(payload)

        db = root / "build.sqlite3"
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE t (x INTEGER)")
        c.execute("INSERT INTO t VALUES (42)")
        c.commit(); c.close()

        cache = StageCache(sources)
        cache.save("demo", db, sig, stats={"answer": 42})
        assert cache.valid("demo", sig)
        restored = root / "restored.sqlite3"
        meta = cache.restore("demo", restored)
        assert meta["stats"]["answer"] == 42
        c = sqlite3.connect(restored)
        assert c.execute("SELECT x FROM t").fetchone()[0] == 42
        c.close()

        # Regression: Windows denies raw copyfile() of a SQLite database after
        # SQLite has acquired an EXCLUSIVE lock.  The live-connection code path
        # must use sqlite3.Connection.backup() instead and must leave the source
        # connection usable after the snapshot.
        live_db = root / "live.sqlite3"
        live = sqlite3.connect(live_db)
        live.execute("PRAGMA locking_mode=EXCLUSIVE")
        live.execute("CREATE TABLE q (x TEXT)")
        live.execute("INSERT INTO q VALUES ('before')")
        live.commit()
        # Force the exclusive connection to touch/read the database before backup.
        assert live.execute("SELECT COUNT(*) FROM q").fetchone()[0] == 1
        cache.save("live", live_db, sig, stats={"mode": "backup"}, source_conn=live)
        assert cache.valid("live", sig)
        # The build may continue using the same connection after a stage snapshot.
        live.execute("INSERT INTO q VALUES ('after')")
        live.commit()
        assert live.execute("SELECT COUNT(*) FROM q").fetchone()[0] == 2
        live.close()
        restored_live = root / "restored-live.sqlite3"
        live_meta = cache.restore("live", restored_live)
        assert live_meta["stats"]["mode"] == "backup"
        c = sqlite3.connect(restored_live)
        assert c.execute("SELECT x FROM q ORDER BY rowid").fetchall() == [("before",)]
        c.close()

        # Final StarDict artifact cache: restoring must create independent copies,
        # not hard links that could be corrupted by a later rebuild.
        out = root / "out"
        out.mkdir()
        base = out / "ru-max-clean"
        base.with_suffix(".ifo").write_text("ifo", encoding="utf-8")
        base.with_suffix(".idx").write_bytes(b"idx" * 100)
        base.with_suffix(".dict").write_bytes(b"dict" * 100)
        ac = ArtifactCache(sources)
        astats = {"wordcount": 123, "dict_bytes": 400, "idx_bytes": 300}
        qreport = out / "QUALITY_REPORT.txt"
        qreport.write_text("quality", encoding="utf-8")
        ac.save("stardict-max", sig, base, stats=astats, extra_files=[qreport])
        assert ac.valid("stardict-max", sig)
        restore_dir = root / "restored-artifacts"
        got = ac.restore("stardict-max", sig, restore_dir)
        assert got and got["wordcount"] == 123
        assert (restore_dir / "ru-max-clean.idx").read_bytes() == b"idx" * 100
        assert (restore_dir / "QUALITY_REPORT.txt").read_text(encoding="utf-8") == "quality"
        # Mutating the restored output must not touch the cache.
        (restore_dir / "ru-max-clean.idx").write_bytes(b"changed")
        restore_dir2 = root / "restored-artifacts-2"
        ac.restore("stardict-max", sig, restore_dir2)
        assert (restore_dir2 / "ru-max-clean.idx").read_bytes() == b"idx" * 100

        source.write_bytes(b"abd" * 100)
        sig2 = signature({"rules": "x1", "source": file_fingerprint(source)})
        assert sig2 != sig and not cache.valid("demo", sig2)
        assert not ac.valid("stardict-max", sig2)
    print("STAGE CACHE TESTS PASSED")


if __name__ == "__main__":
    main()
