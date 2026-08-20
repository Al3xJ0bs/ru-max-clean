"""Single source of truth for public and internal RU Max Clean versions."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _read_first(path: Path, fallback: str) -> str:
    try:
        value = path.read_text(encoding="utf-8-sig").strip().split()
    except OSError:
        return fallback
    return value[0] if value else fallback


BUILDER_VERSION = _read_first(ROOT / "VERSION.txt", "unknown")
PUBLIC_VERSION = _read_first(ROOT / "PUBLIC_VERSION.txt", "unknown")
