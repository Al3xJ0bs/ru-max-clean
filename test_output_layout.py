#!/usr/bin/env python3
"""Regression tests for the generated-dictionary directory layout."""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path
import tempfile

from build_reader_packs import output_dir_from_args, output_layout_label, parse_args
from dictionary_layout import (
    CORE_DIR_NAME,
    DICTIONARIES_ROOT_NAME,
    READER_COVERAGE_DIR_NAME,
    READER_PACKS_DIR_NAME,
    core_dir,
    dictionaries_root,
    existing_or_canonical,
    legacy_core_dir,
    reader_coverage_dir,
    reader_packs_dir,
)
from ru_max_launcher import (
    CORE_OUTPUT,
    OUTPUT_ROOT,
    READER_COVERAGE_OUTPUT,
    READER_PACKS_OUTPUT,
)


def main() -> None:
    assert OUTPUT_ROOT.name == DICTIONARIES_ROOT_NAME
    assert CORE_OUTPUT.parent == OUTPUT_ROOT
    assert READER_PACKS_OUTPUT.parent == OUTPUT_ROOT
    assert READER_COVERAGE_OUTPUT.parent == OUTPUT_ROOT
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = dictionaries_root(base)
        assert root == base / DICTIONARIES_ROOT_NAME
        assert core_dir(base) == root / CORE_DIR_NAME
        assert reader_packs_dir(base) == root / READER_PACKS_DIR_NAME
        assert reader_coverage_dir(base) == root / READER_COVERAGE_DIR_NAME

        # New invocations use the unified root, while explicit output-dir keeps
        # old scripts and installations working unchanged.
        assert output_dir_from_args(Namespace(output_dir=None, output_root=base)) == (
            base / READER_PACKS_DIR_NAME
        )
        assert output_dir_from_args(parse_args([])) == dictionaries_root() / READER_PACKS_DIR_NAME
        old = base / READER_PACKS_DIR_NAME
        assert output_dir_from_args(Namespace(output_dir=old, output_root=None)) == old
        assert output_layout_label(old) == "custom"

        assert existing_or_canonical(core_dir(base), legacy_core_dir(base)) == core_dir(base)
        legacy_core_dir(base).mkdir(parents=True)
        assert existing_or_canonical(core_dir(base), legacy_core_dir(base)) == legacy_core_dir(base)
        core_dir(base).mkdir(parents=True)
        assert existing_or_canonical(core_dir(base), legacy_core_dir(base)) == core_dir(base)

    print("OUTPUT LAYOUT TESTS PASSED")


if __name__ == "__main__":
    main()
