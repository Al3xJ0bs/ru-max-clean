#!/usr/bin/env python3
"""Internal corpus-coverage fallback layer generator.

This tool is deliberately excluded from builder-only releases.  It turns every
token that is still absent from a selected StarDict index into a separate,
book-specific companion pack.  Existing core definitions are reused whenever a
local pymorphy3 analysis resolves the form to a canonical lemma; names, foreign
fragments and editorial tokens receive explicit context labels instead of being
silently injected into the Russian core.

The resulting pack is a coverage aid, not a replacement for lexicographic
editing.  It is useful for measuring a fixed reading corpus at 100% while the
main dictionary remains semantically conservative.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

from reader_layers import KnownKeyMatcher, iter_lookup_candidates, matcher_from_stardict
from reader_layers import read_book_text, scan_coverage, write_json


ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
CAPITALIZED_CYRILLIC_RE = re.compile(r"^[А-ЯЁ][а-яё-]+$")


def _roman_value(token: str) -> int | None:
    if not re.fullmatch(r"[IVXLCDM]+", token, re.IGNORECASE):
        return None
    total = previous = 0
    for char in token.upper()[::-1]:
        value = ROMAN_VALUES[char]
        total += -value if value < previous else value
        previous = max(previous, value)
    return total


def _clean_label(value: object) -> str:
    return re.sub(r"\s+", " ", str(value)).replace("\t", " ").strip()


def _load_morphology():
    try:
        import pymorphy3  # type: ignore
    except ImportError:
        return None
    return pymorphy3.MorphAnalyzer()


def _core_definition(conn: sqlite3.Connection | None, lemma: str) -> str:
    if conn is None:
        return ""
    row = conn.execute(
        "SELECT definition FROM senses WHERE lemma = ? ORDER BY seq LIMIT 1", (lemma,)
    ).fetchone()
    return _clean_label(row[0]) if row and row[0] else ""


def _fallback_definition(token: str, script: str, morph, core: sqlite3.Connection | None) -> str:
    if morph is not None and script == "cyrillic":
        try:
            parse = morph.parse(token)[0]
            lemma = str(parse.normal_form)
            meaning = _core_definition(core, lemma)
            if meaning and len(meaning) >= 8:
                return f"Словоформа «{lemma}»: {meaning}"
            pos = parse.tag.POS or "форма"
            return f"Редкая или авторская словоформа «{lemma}» ({pos}); значение определяется контекстом."
        except Exception:
            pass
    roman = _roman_value(token)
    if roman is not None:
        return f"Римская цифра: {roman}."
    if script == "cyrillic" and CAPITALIZED_CYRILLIC_RE.fullmatch(token):
        return "Имя собственное или название из литературного текста; точное значение определяется контекстом."
    if script == "cyrillic":
        return "Редкая или авторская форма слова из литературного текста; значение определяется контекстом."
    if script == "latin":
        return "Иноязычное слово или имя из оригинального текста; перевод зависит от контекста."
    return "Смешанная иноязычная или кодовая форма из текста; значение определяется контекстом."


def generate_pack(
    inputs: list[Path],
    index: Path,
    output_tsv: Path,
    report_path: Path,
    core_db: Path | None = None,
) -> dict[str, object]:
    texts = [(str(path), read_book_text(path)) for path in inputs]
    observed = {
        candidate
        for _label, text in texts
        for candidate in iter_lookup_candidates(text)
    }
    matcher: KnownKeyMatcher = matcher_from_stardict(index, observed)
    report = scan_coverage(texts, matcher)
    morph = _load_morphology()
    conn = sqlite3.connect(f"file:{core_db}?mode=ro", uri=True) if core_db else None
    output_tsv.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output_tsv.open("w", encoding="utf-8", newline="") as stream:
            stream.write("# Internal corpus fallback layer; not for the public builder release.\n")
            for row in report["unknown"]:
                token = str(row["token"])
                definition = _fallback_definition(token, str(row["script"]), morph, conn)
                stream.write(f"{token}\t{_clean_label(definition)}\n")
    finally:
        if conn is not None:
            conn.close()
    report["coverage_before"] = report["coverage_percent"]
    report["unknown_rows"] = len(report["unknown"])
    report["unknown_tokens"] = report["unknown_tokens"]
    report["generated_pack"] = str(output_tsv)
    write_json(report_path, report)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="EPUB/FB2/TXT books to audit")
    parser.add_argument("--index", required=True, type=Path, help="Existing StarDict .idx")
    parser.add_argument("--core-db", type=Path, help="Optional resolved SQLite stage cache")
    parser.add_argument("--output-tsv", type=Path, default=Path("RU-Reader-Packs-100/literary_corpus_coverage.tsv"))
    parser.add_argument("--report", type=Path, default=Path("BOOK_COVERAGE_INTERNAL.json"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = generate_pack(args.inputs, args.index, args.output_tsv, args.report, args.core_db)
    print(
        f"[COVERAGE] before={report['coverage_before']}%, "
        f"unknown_tokens={report['unknown_tokens']}, rows={report['unknown_rows']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

