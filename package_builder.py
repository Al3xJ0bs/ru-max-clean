#!/usr/bin/env python3
"""Create a deterministic builder-only ZIP for a public GitHub release.

The repository intentionally contains source fixtures and reader-pack inputs.
Releases ship the small original dictionary test book, but never ship generated
dictionaries, user books, caches, or downloaded dumps.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path
from typing import Iterable


EXCLUDED_DIRS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "sources",
    "dist",
    "_test_output",
    "RU-Max-Clean",
    "RU-Max-Clean-Production",
    "RU-Reader-Packs",
    "RU-Reader-Packs-100",
    "RU-Dictionaries",
}
EXCLUDED_PREFIXES = ("RU-Max-Clean-", "RU-Reader-Packs-")
EXCLUDED_SUFFIXES = {
    ".dict",
    ".idx",
    ".ifo",
    ".syn",
    ".zip",
    ".epub",
    ".fb2",
    ".log",
}
EXCLUDED_FILES = {
    # Corpus scanner and its reports are internal QA tools, not part of the
    # end-user builder release.
    "scan_book_coverage.py",
    "reader_layers.py",
    "internal_book_coverage.py",
    "test_reader_layers.py",
    "BOOK_COVERAGE_NEW.json",
    "BOOK_COVERAGE_WITH_LAYERS.json",
    "BUILD_STATS.json",
    "BUILD_INFO.json",
    "QUALITY_REPORT.json",
    "QUALITY_REPORT.txt",
    "QUALITY_REVIEW.tsv",
    "QUALITY_ONOMASTICS.tsv",
    "QUALITY_CONCISE.tsv",
    "SOURCES.txt",
    "test_books/make_dictionary_test_book.py",
}
TEST_BOOK_FILES = {
    "test_books/RU-Max-Clean-Dictionary-Test-Book.epub",
    "test_books/RU-Max-Clean-Dictionary-Test-Book.fb2",
    "test_books/README_RU.txt",
}
# Runtime modules that must ship even when a developer builds directly from a
# working tree before staging newly added files.  ``git ls-files`` is the
# normal source of truth, but omitting an untracked import would produce a ZIP
# that fails immediately after extraction.
RELEASE_REQUIRED_FILES = {
    "dictionary_layout.py",
}
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def _excluded(path: Path) -> bool:
    if path.as_posix() in TEST_BOOK_FILES:
        return False
    if path.is_symlink():
        return True
    if any(part in EXCLUDED_DIRS for part in path.parts):
        return True
    if path.name.startswith(EXCLUDED_PREFIXES):
        return True
    if path.name in EXCLUDED_FILES or path.as_posix() in EXCLUDED_FILES:
        return True
    if path.name.startswith("BOOK_COVERAGE_"):
        return True
    return path.suffix.lower() in EXCLUDED_SUFFIXES


def _git_files(root: Path) -> list[Path]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    names = proc.stdout.decode("utf-8").split("\0")
    return [root / name for name in names if name]


def package_files(root: Path) -> list[Path]:
    """Return the deterministic, builder-only source whitelist."""
    candidates = _git_files(root)
    if not candidates:
        candidates = [p for p in root.rglob("*") if p.is_file()]
    else:
        tracked_names = {p.relative_to(root).as_posix() for p in candidates}
        for relative in RELEASE_REQUIRED_FILES:
            path = root / relative
            if path.exists() and relative not in tracked_names:
                candidates.append(path)
    return sorted(
        (p for p in candidates if p.exists() and p.is_file() and not _excluded(p.relative_to(root))),
        key=lambda p: p.relative_to(root).as_posix(),
    )


def _builder_version(root: Path) -> str:
    version_file = root / "VERSION.txt"
    if not version_file.exists():
        return "unknown"
    return version_file.read_text(encoding="utf-8-sig").strip().split()[0]


def _public_version(root: Path) -> str:
    version_file = root / "PUBLIC_VERSION.txt"
    if not version_file.exists():
        return "unknown"
    return version_file.read_text(encoding="utf-8-sig").strip().split()[0]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _zip_entry(name: str, data: bytes | None = None) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def build_package(root: Path, output: Path, public_version: str) -> dict[str, object]:
    root = root.resolve()
    output = output.resolve()
    files = package_files(root)
    manifest_files: list[dict[str, object]] = []
    payloads: list[tuple[str, bytes]] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        data = path.read_bytes()
        payloads.append((relative, data))
        manifest_files.append({"path": relative, "bytes": len(data), "sha256": _sha256(data)})

    manifest = {
        "manifest_version": 1,
        "public_version": public_version,
        "builder_version": _builder_version(root),
        "package_kind": "builder-only",
        "files": manifest_files,
    }
    manifest_data = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr(_zip_entry("BUILD_MANIFEST.json", manifest_data), manifest_data)
        for relative, data in payloads:
            archive.writestr(_zip_entry(relative), data)

    digest = _sha256(output.read_bytes())
    sidecar = output.with_name(output.name + ".sha256")
    sidecar.write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    return {
        "output": str(output),
        "sha256": digest,
        "file_count": len(payloads),
        "builder_version": manifest["builder_version"],
        "public_version": public_version,
        "manifest": manifest,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument(
        "--public-version",
        default=None,
        help="Public release version (defaults to PUBLIC_VERSION.txt)",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    public_version = args.public_version or _public_version(root)
    output = args.output or root / "dist" / f"RU-Max-Clean-Builder-v{public_version}.zip"
    result = build_package(root, output, public_version)
    print(json.dumps({k: v for k, v in result.items() if k != "manifest"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
