#!/usr/bin/env python3
"""Build optional literary/foreign-language StarDict companion packs.

The core RU-Max-Clean build is intentionally untouched.  Each TSV in
``reader_packs`` becomes its own ``ru-max-clean`` StarDict triplet in a named
subdirectory, so KOReader users can enable only the layers useful for a book.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys

from build_ru_max_clean import (
    add_link,
    build_stardict,
    clean_definition,
    connect_db,
    is_lookup_key,
    normalize_key,
)
from reader_pack_loader import PackEntry, load_pack_tsv


PACK_METADATA: dict[str, dict[str, str]] = {
    "latin_classical": {"language": "la", "kind": "classical phrases and terms"},
    "latin_wiktionary": {"language": "la", "kind": "Russian-glossed Latin lexemes"},
    "literary_archaic": {"language": "cu/orv/ru-old", "kind": "curated historical vocabulary"},
    "literary_wiktionary": {"language": "cu/orv/ru-old", "kind": "Russian-glossed historical lexemes"},
    "phraseology": {"language": "ru", "kind": "fixed expressions"},
    "french_literary": {"language": "fr", "kind": "French words and expressions in Russian literary prose"},
    "literary_names": {"language": "ru", "kind": "proper names and places from Russian literary reading"},
    "fantasy_terms": {"language": "ru", "kind": "fantasy terminology and world-specific concepts"},
    "literary_terms": {"language": "ru", "kind": "historical and culture-specific literary terms"},
    "literary_abbreviations": {"language": "ru", "kind": "editorial abbreviations common in literary editions"},
}


def _pack_slug(path: Path) -> str:
    return path.stem.casefold().replace("-", "_").replace(" ", "_")


def _insert_entries(conn: sqlite3.Connection, entries: list[PackEntry], source: str) -> dict[str, int]:
    accepted = 0
    rejected = 0
    aliases = 0
    for entry in entries:
        word = normalize_key(entry.word)
        definition = clean_definition(entry.definition)
        if not is_lookup_key(word) or not definition:
            rejected += 1
            continue
        conn.execute(
            "INSERT OR IGNORE INTO senses(lemma, definition, source) VALUES (?, ?, ?)",
            (word, definition, source),
        )
        add_link(conn, word, word)
        for alias in entry.aliases:
            alias = normalize_key(alias)
            if is_lookup_key(alias):
                add_link(conn, alias, word)
                aliases += 1
        accepted += 1
    conn.commit()
    return {"entries": accepted, "rejected": rejected, "aliases": aliases}


def build_pack(path: Path, output_root: Path, *, bookname: str | None = None) -> dict[str, object]:
    entries = load_pack_tsv(path)
    slug = _pack_slug(path)
    out_dir = output_root / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = connect_db(Path(":memory:"))
    try:
        stats = _insert_entries(conn, entries, f"reader:{slug}")
        result = build_stardict(conn, out_dir, bookname=bookname or f"RU Reader — {slug}")
    finally:
        conn.close()
    payload = {
        "pack_version": "1.0",
        "pack": slug,
        **PACK_METADATA.get(slug, {"language": "und", "kind": "custom reader layer"}),
        "source_file": path.name,
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "stats": stats,
        "stardict": result,
        "license_note": "Curated seed data; see READER_LAYERS_RU.md and the pack source comments.",
    }
    (out_dir / "PACK_INFO.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build RU Max Clean optional reader dictionaries")
    parser.add_argument("--pack-dir", default="reader_packs", help="Directory containing *.tsv packs")
    parser.add_argument("--output-dir", default="RU-Reader-Packs", help="Output directory for companion packs")
    parser.add_argument("--pack", action="append", help="Build only this TSV filename (repeatable)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pack_dir = Path(args.pack_dir)
    output_dir = Path(args.output_dir)
    if args.pack:
        paths = [pack_dir / name for name in args.pack]
    else:
        paths = sorted(pack_dir.glob("*.tsv"))
    if not paths:
        raise SystemExit(f"No reader packs found in {pack_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, object]] = []
    for path in paths:
        if not path.exists():
            raise SystemExit(f"Pack not found: {path}")
        print(f"[READER PACK] {path.name}", flush=True)
        manifest.append(build_pack(path, output_dir))
    (output_dir / "PACKS_MANIFEST.json").write_text(
        json.dumps({"pack_version": "1.0", "packs": manifest}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[READER PACK] built {len(manifest)} pack(s) in {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

