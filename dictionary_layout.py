"""Canonical layout for generated RU Max Clean dictionaries.

Generated dictionaries used to be written directly next to the builder.  Keep
the directory names inside one small module so the launcher, companion-pack
builder, and internal coverage tooling agree on where a completed build lives.
The old top-level paths are intentionally exposed as migration fallbacks; a
user may keep using an explicit ``--output-dir`` without having to move a
large existing dictionary first.
"""
from __future__ import annotations

from pathlib import Path


DICTIONARIES_ROOT_NAME = "RU-Dictionaries"
CORE_DIR_NAME = "RU-Max-Clean"
READER_PACKS_DIR_NAME = "RU-Reader-Packs"
READER_COVERAGE_DIR_NAME = "RU-Reader-Packs-100"


def dictionaries_root(base: Path | None = None) -> Path:
    """Return the canonical generated-artifacts root for *base*.

    ``base`` is normally the builder directory.  Keeping this function
    path-based (rather than using a process-global constant) makes it easy for
    tests and callers embedding the builder to select a temporary workspace.
    """

    return (Path(base) if base is not None else Path.cwd()) / DICTIONARIES_ROOT_NAME


def core_dir(base: Path | None = None) -> Path:
    return dictionaries_root(base) / CORE_DIR_NAME


def reader_packs_dir(base: Path | None = None) -> Path:
    return dictionaries_root(base) / READER_PACKS_DIR_NAME


def reader_coverage_dir(base: Path | None = None) -> Path:
    return dictionaries_root(base) / READER_COVERAGE_DIR_NAME


def legacy_core_dir(base: Path | None = None) -> Path:
    return (Path(base) if base is not None else Path.cwd()) / CORE_DIR_NAME


def legacy_reader_packs_dir(base: Path | None = None) -> Path:
    return (Path(base) if base is not None else Path.cwd()) / READER_PACKS_DIR_NAME


def legacy_reader_coverage_dir(base: Path | None = None) -> Path:
    return (Path(base) if base is not None else Path.cwd()) / READER_COVERAGE_DIR_NAME


def existing_or_canonical(canonical: Path, legacy: Path) -> Path:
    """Use a legacy output only when the canonical one is not present.

    This is deliberately read-only.  Moving/copying a multi-gigabyte StarDict
    is an explicit user decision, while validation and quick launches can keep
    working immediately after an upgrade.
    """

    if canonical.exists() or not legacy.exists():
        return canonical
    return legacy


__all__ = [
    "CORE_DIR_NAME",
    "DICTIONARIES_ROOT_NAME",
    "READER_COVERAGE_DIR_NAME",
    "READER_PACKS_DIR_NAME",
    "core_dir",
    "dictionaries_root",
    "existing_or_canonical",
    "legacy_core_dir",
    "legacy_reader_coverage_dir",
    "legacy_reader_packs_dir",
    "reader_coverage_dir",
    "reader_packs_dir",
]
