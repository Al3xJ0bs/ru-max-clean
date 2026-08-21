#!/usr/bin/env python3
"""Regression tests for the numeric launcher choices."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import ru_max_launcher as launcher
from ru_max_launcher import READER_PACKS, parse_profile_choice, parse_reader_pack_choice


def main() -> None:
    assert parse_reader_pack_choice("") == []
    assert parse_reader_pack_choice("0") == []
    assert parse_reader_pack_choice("1") == [name for name, _label in READER_PACKS]
    assert parse_reader_pack_choice("2") == [READER_PACKS[0][0]]
    assert parse_reader_pack_choice("2 4 2") == [READER_PACKS[0][0], READER_PACKS[2][0]]
    assert parse_reader_pack_choice("A") is None
    assert parse_reader_pack_choice("все") is None
    assert parse_reader_pack_choice("0 2") is None
    assert parse_reader_pack_choice("99") is None
    assert parse_profile_choice("") == "max"
    assert parse_profile_choice("1") == "max"
    assert parse_profile_choice("2") == "lexical"
    assert parse_profile_choice("max") is None

    # A legacy dictionary is readable/validatable, but must not make smart
    # build skip creation of the canonical output tree.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        canonical = root / "RU-Dictionaries" / "RU-Max-Clean"
        legacy = root / "RU-Max-Clean"
        legacy.mkdir(parents=True)
        for ext in ("ifo", "idx", "dict"):
            (legacy / f"ru-max-clean.{ext}").write_bytes(b"x")
        old = (launcher.OUT, launcher.LEGACY_OUT, launcher.STATE, launcher.SOURCES)
        try:
            launcher.OUT = canonical
            launcher.LEGACY_OUT = legacy
            launcher.STATE = root / "state.json"
            launcher.SOURCES = root / "sources"
            launcher.SOURCES.mkdir()
            launcher.STATE.write_text(
                json.dumps({
                    "version": launcher.VERSION,
                    "profile": "max",
                    "sources": {},
                    "local_build_fingerprint": launcher.local_build_fingerprint(),
                }),
                encoding="utf-8",
            )
            assert launcher.build_files_exist()
            assert not launcher.is_current("max")
            canonical.mkdir(parents=True)
            for ext in ("ifo", "idx", "dict"):
                (canonical / f"ru-max-clean.{ext}").write_bytes(b"x")
            assert launcher.is_current("max")
        finally:
            launcher.OUT, launcher.LEGACY_OUT, launcher.STATE, launcher.SOURCES = old
    print("LAUNCHER TESTS PASSED")


if __name__ == "__main__":
    main()
