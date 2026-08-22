#!/usr/bin/env python3
"""Regression tests for multi-layer book-coverage scans."""
from __future__ import annotations

import json
import struct
import tempfile
from pathlib import Path

from scan_book_coverage import main


def _write_idx(path: Path, keys: list[str]) -> None:
    with path.open("wb") as stream:
        for key in keys:
            stream.write(key.encode("utf-8") + b"\0" + struct.pack("!II", 0, 0))


def test_multiple_indexes_are_combined() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        book = root / "book.txt"
        first = root / "core.idx"
        second = root / "companion.idx"
        report_path = root / "coverage.json"
        book.write_text("Слово lōrem", encoding="utf-8")
        _write_idx(first, ["слово"])
        _write_idx(second, ["lōrem"])

        assert main([
            str(book), "--index", str(first), "--index", str(second),
            "--output", str(report_path),
        ]) == 0
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["coverage_percent"] == 100.0
        assert report["unknown_tokens"] == 0
        assert report["indexes"] == [str(first), str(second)]


def test_phrase_key_is_retained_without_materialising_corpus_ngrams() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        book = root / "book.txt"
        index = root / "core.idx"
        report_path = root / "coverage.json"
        book.write_text("Истинный смысл", encoding="utf-8")
        _write_idx(index, ["истинный смысл"])

        assert main([
            str(book), "--index", str(index), "--output", str(report_path),
        ]) == 0
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["coverage_percent"] == 100.0
        assert report["unknown_tokens"] == 0


if __name__ == "__main__":
    test_multiple_indexes_are_combined()
    test_phrase_key_is_retained_without_materialising_corpus_ngrams()
    print("BOOK COVERAGE SCAN TESTS PASSED")
