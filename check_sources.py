#!/usr/bin/env python3
from __future__ import annotations
import argparse
from pathlib import Path

from build_ru_max_clean import (
    DAL_URLS,
    KAIKKI_URL,
    OPENCORPORA_URLS,
    RUWIKI_URL,
    WIKIDATA_LEXEMES_URL,
)
from source_manager import SourceCache


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", choices=("max", "lexical"), default="max")
    ap.add_argument("--force-refresh", action="store_true")
    args = ap.parse_args()
    root = Path(__file__).resolve().parent
    cache_dir = root / "sources"
    cache = SourceCache(cache_dir, force_refresh=args.force_refresh)
    specs = [
        ("Kaikki / Russian Wiktionary", KAIKKI_URL, cache_dir / "raw-wiktextract-data.jsonl.gz", True),
        # 4.6 changed OpenCorpora to a mirror list.  Keep the source check in
        # lock-step with the builder; importing the old singular constant made
        # menu item 1 fail before any build work started.
        ("OpenCorpora morphology", OPENCORPORA_URLS, cache_dir / "dict.opcorpora.xml.bz2", False),
        ("Wikidata Lexemes", WIKIDATA_LEXEMES_URL, cache_dir / "latest-lexemes.json.bz2", False),
        ("Dal historical dictionary", DAL_URLS, cache_dir / "stardict-dal-ru-2.4.2.tar.bz2", False),
    ]
    if args.profile == "max":
        specs.append(("Russian Wikipedia terminology", RUWIKI_URL, cache_dir / "ruwiki-latest-pages-articles.xml.bz2", False))
    for label, urls, destination, required in specs:
        cache.ensure(label, urls, destination, required=required)
    print(f"Source check complete ({args.profile}). No dictionary rebuild was performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
