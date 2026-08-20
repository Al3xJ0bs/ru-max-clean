#!/usr/bin/env python3
"""Find frequent book tokens missing from an existing RU-Max-Clean index."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from reader_layers import KnownKeyMatcher, read_book_text, scan_coverage, write_json


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan Russian books against a StarDict index")
    parser.add_argument("inputs", nargs="+", type=Path, help="TXT/HTML/EPUB/GZ book files")
    parser.add_argument("--index", type=Path, help="Existing StarDict .idx file")
    parser.add_argument("--known-keys", type=Path, help="UTF-8 newline-separated key list (alternative to --index)")
    parser.add_argument("--output", type=Path, default=Path("BOOK_COVERAGE.json"))
    parser.add_argument("--top", type=int, default=5000, help="Maximum unknown rows to retain")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if bool(args.index) == bool(args.known_keys):
        raise SystemExit("Specify exactly one of --index or --known-keys")
    texts = []
    for path in args.inputs:
        if not path.exists():
            raise SystemExit(f"Input not found: {path}")
        print(f"[COVERAGE] reading {path}", flush=True)
        texts.append((str(path), read_book_text(path)))
    # First pass is intentionally independent of the dictionary so an index
    # can be streamed only for observed words, rather than loaded wholesale.
    if args.index:
        from reader_layers import iter_lookup_candidates, matcher_from_stardict
        observed = {
            candidate
            for _label, text in texts
            for candidate in iter_lookup_candidates(text)
        }
        matcher = matcher_from_stardict(args.index, observed)
    else:
        keys = args.known_keys.read_text(encoding="utf-8", errors="replace").splitlines()
        matcher = KnownKeyMatcher(keys)
    report = scan_coverage(texts, matcher)
    if args.top > 0:
        report["unknown"] = report["unknown"][:args.top]
    report["index"] = str(args.index) if args.index else str(args.known_keys)
    write_json(args.output, report)
    print(
        f"[COVERAGE] {report['tokens_total']} tokens, coverage {report['coverage_percent']}%, "
        f"unknown rows saved: {len(report['unknown'])} -> {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
