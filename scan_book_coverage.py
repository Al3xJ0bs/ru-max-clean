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
    parser.add_argument(
        "--index",
        action="append",
        type=Path,
        help="StarDict .idx file; repeat the option to audit the core together with selected companion layers",
    )
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
    # Collect only single tokens here.  The previous implementation stored
    # every two-to-four-word n-gram in a 25-book corpus (tens of millions of
    # transient strings), causing needless multi-gigabyte memory use.  Phrase
    # keys are instead retained while each index is streamed below.
    if args.index:
        from reader_layers import iter_tokens, matcher_from_stardict, normalize_lookup
        observed = {
            normal
            for _label, text in texts
            for token in iter_tokens(text)
            for normal in (normalize_lookup(token),)
            if normal and any(char.isalpha() for char in token)
        }
        matched_keys: set[str] = set()
        for index in args.index:
            if not index.exists():
                raise SystemExit(f"Index not found: {index}")
            # Each large index is streamed only against corpus candidates.  The
            # union is therefore bounded by the observed book vocabulary, not
            # by all keys in every installed dictionary.
            matched_keys.update(
                matcher_from_stardict(index, observed, collect_short_phrases=True).keys
            )
        matcher = KnownKeyMatcher(matched_keys)
    else:
        keys = args.known_keys.read_text(encoding="utf-8", errors="replace").splitlines()
        matcher = KnownKeyMatcher(keys)
    report = scan_coverage(texts, matcher)
    if args.top > 0:
        report["unknown"] = report["unknown"][:args.top]
    report["indexes"] = [str(path) for path in args.index] if args.index else [str(args.known_keys)]
    write_json(args.output, report)
    print(
        f"[COVERAGE] {report['tokens_total']} tokens, coverage {report['coverage_percent']}%, "
        f"unknown rows saved: {len(report['unknown'])} -> {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
