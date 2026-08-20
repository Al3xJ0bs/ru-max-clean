#!/usr/bin/env python3
"""Small local decompression benchmark for RU Max Clean cached sources."""
from __future__ import annotations
import bz2
import gzip
import os
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
SOURCES = BASE / "sources"
LIMIT = 128 * 1024 * 1024
CHUNK = 4 * 1024 * 1024

try:
    import rapidgzip
except Exception:
    rapidgzip = None
try:
    import indexed_bzip2
except Exception:
    indexed_bzip2 = None


def consume(open_fn, path: Path, *, limit: int = LIMIT) -> tuple[float, int]:
    total = 0
    t0 = time.perf_counter()
    with open_fn(path) as fh:
        while total < limit:
            data = fh.read(min(CHUNK, limit - total))
            if not data:
                break
            total += len(data)
    elapsed = max(1e-9, time.perf_counter() - t0)
    return elapsed, total


def show(label: str, elapsed: float, total: int) -> float:
    mib = total / (1024 * 1024)
    speed = mib / elapsed
    print(f"  {label:<28} {speed:8.1f} MiB/s   ({elapsed:.2f} s)")
    return speed


def main() -> int:
    print("=" * 68)
    print(" ТЕСТ ЛОКАЛЬНОЙ ДЕКОМПРЕССИИ")
    print("=" * 68)
    print("Читаются только первые 128 MiB распакованного потока; файлы не изменяются.\n")

    gz = SOURCES / "raw-wiktextract-data.jsonl.gz"
    if gz.exists():
        print("Kaikki / gzip:")
        e, n = consume(lambda p: gzip.open(p, "rb"), gz)
        base = show("stdlib gzip", e, n)
        if rapidgzip is not None:
            e, n = consume(lambda p: rapidgzip.open(str(p), parallelization=max(1, os.cpu_count() or 1)), gz)
            fast = show("rapidgzip / all CPU", e, n)
            if base:
                print(f"  Ускорение: x{fast / base:.2f}")
        else:
            print("  rapidgzip: не установлен")
        print()
    else:
        print("Kaikki .gz не найден; gzip-тест пропущен.\n")

    bz = SOURCES / "latest-lexemes.json.bz2"
    if bz.exists():
        print("Wikidata / bzip2:")
        e, n = consume(lambda p: bz2.open(p, "rb"), bz)
        base = show("stdlib bz2", e, n)
        if indexed_bzip2 is not None:
            e, n = consume(
                lambda p: indexed_bzip2.open(str(p), parallelization=max(2, min(32, os.cpu_count() or 2))),
                bz,
            )
            fast = show("indexed_bzip2 / all CPU", e, n)
            if base:
                print(f"  Ускорение: x{fast / base:.2f}")
        else:
            print("  indexed_bzip2: не установлен; используется stdlib bz2")
        print()
    else:
        print("Wikidata .bz2 не найден; bzip2-тест пропущен.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
