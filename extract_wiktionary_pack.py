#!/usr/bin/env python3
"""Extract a conservative reader pack from the multilingual Kaikki dump.

Russian Wiktionary contains useful Latin and Church Slavonic entries, but the
raw dump also contains inflection-only rows and example prose.  This extractor
keeps only short Russian glosses and deliberately leaves the main RU-Max-Clean
pipeline untouched.
"""
from __future__ import annotations

import argparse
import gzip
import html
import json
from pathlib import Path
import re

from build_ru_max_clean import is_lookup_key, normalize_key


GRAMMAR_ONLY_RE = re.compile(
    r"^(?:форма|формы|родительный|дательный|винительный|творительный|предложный|"
    r"именительный|множественное|единственное)\b",
    re.IGNORECASE,
)
META_RE = re.compile(r"\s+")
PLACEHOLDER_RE = re.compile(r"^(?:сокр(?:ащение)?\.?|аббр\.?|имя\s+собственное\.?)$", re.IGNORECASE)


def clean_gloss(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = html.unescape(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"<[^>]+>", "", text)
    # Wiktionary glosses occasionally append examples or template markup to a
    # valid translation.  Keep the meaning before the marker and never expose
    # the source markup in a popup.
    for marker in ("◆", "◇", "{{", "[[", "[НКРЯ]"):
        if marker in text:
            text = text.split(marker, 1)[0]
    text = META_RE.sub(" ", text).strip(" .;,:—–-")
    if not text or len(text) > 320 or GRAMMAR_ONLY_RE.match(text) or PLACEHOLDER_RE.fullmatch(text):
        return ""
    if "аналогично русскому слову" in text.casefold() or text.casefold() in {"см.", "см"}:
        return ""
    return text


def extract(input_path: Path, output_path: Path, languages: set[str], max_defs: int) -> dict[str, int]:
    rows: dict[str, list[str]] = {}
    records = 0
    accepted = 0
    skipped = 0
    with gzip.open(input_path, "rt", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            records += 1
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if data.get("lang_code") not in languages:
                continue
            word = normalize_key(str(data.get("word") or ""))
            if not is_lookup_key(word):
                skipped += 1
                continue
            bucket = rows.setdefault(word, [])
            for sense in data.get("senses") or []:
                tags = {str(tag).casefold() for tag in (sense.get("tags") or [])}
                if tags & {"form-of", "inflection", "no-gloss", "no definition"}:
                    continue
                for gloss in sense.get("glosses") or []:
                    text = clean_gloss(gloss)
                    if text and text not in bucket and len(bucket) < max_defs:
                        bucket.append(text)
                        accepted += 1
            if not bucket:
                rows.pop(word, None)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as out:
        out.write("# Extracted from Russian Wiktionary/Kaikki; review before redistribution.\n")
        for word in sorted(rows, key=str.casefold):
            for definition in rows[word]:
                out.write(f"{word}\t{definition}\t\n")
    return {
        "records_processed": records,
        "words_written": len(rows),
        "definitions_written": sum(len(items) for items in rows.values()),
        "glosses_accepted": accepted,
        "rows_skipped": skipped,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract optional reader TSV from Kaikki raw JSONL.GZ")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--languages", default="la", help="Comma-separated Wiktionary language codes")
    parser.add_argument("--max-definitions", type=int, default=3)
    args = parser.parse_args(argv)
    stats = extract(args.input, args.output, {item.strip() for item in args.languages.split(",") if item.strip()}, args.max_definitions)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
