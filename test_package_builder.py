#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import tempfile
import zipfile
from pathlib import Path

from package_builder import build_package


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "VERSION.txt").write_text("4.9.1 TURBO\n", encoding="utf-8")
        (root / "build_ru_max_clean.py").write_text("print('builder')\n", encoding="utf-8")
        (root / "reader_packs").mkdir()
        (root / "reader_packs" / "phraseology.tsv").write_text("a\tb\n", encoding="utf-8")
        (root / "scan_book_coverage.py").write_text("print('internal')\n", encoding="utf-8")
        (root / "reader_layers.py").write_text("print('internal scanner')\n", encoding="utf-8")
        (root / "internal_book_coverage.py").write_text("print('internal coverage')\n", encoding="utf-8")
        (root / "test_reader_layers.py").write_text("print('internal scanner tests')\n", encoding="utf-8")
        (root / "BOOK_COVERAGE_NEW.json").write_text("{}\n", encoding="utf-8")
        (root / "RU-Max-Clean-4.9.1-PRODUCTION").mkdir()
        (root / "RU-Max-Clean-4.9.1-PRODUCTION" / "ru-max-clean.dict").write_bytes(b"not shipped")
        (root / "book.epub").write_bytes(b"not shipped")

        first = root / "dist" / "builder.zip"
        second = root / "dist" / "builder-again.zip"
        result = build_package(root, first, "1.0")
        build_package(root, second, "1.0")
        assert result["file_count"] == 3
        assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()

        with zipfile.ZipFile(first) as archive:
            names = archive.namelist()
            assert "BUILD_MANIFEST.json" in names
            assert "build_ru_max_clean.py" in names
            assert "reader_packs/phraseology.tsv" in names
            assert "scan_book_coverage.py" not in names
            assert "reader_layers.py" not in names
            assert "internal_book_coverage.py" not in names
            assert "test_reader_layers.py" not in names
            assert "BOOK_COVERAGE_NEW.json" not in names
            assert not any(name.endswith((".dict", ".epub")) for name in names)
            manifest = json.loads(archive.read("BUILD_MANIFEST.json"))
            assert manifest["package_kind"] == "builder-only"
            assert manifest["public_version"] == "1.0"
            assert manifest["builder_version"] == "4.9.1"

    print("PACKAGE BUILDER TESTS PASSED")


if __name__ == "__main__":
    main()

