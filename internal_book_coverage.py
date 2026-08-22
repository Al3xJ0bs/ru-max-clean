#!/usr/bin/env python3
"""Internal, frequency-first OOV analysis for a fixed reading corpus.

This tool is deliberately excluded from builder-only releases. It compares
books with an existing StarDict index and writes review candidates, never a
fallback dictionary. Writing this report does not add a key, definition,
alias, or any synthetic coverage to the scanned dictionary.

The report separates ordinary Russian lexical candidates from possible names,
foreign fragments, abbreviations, Roman numerals, and a very small set of
conservative typographic-noise patterns. Optional morphology is used only to
group forms and identify forms of a lemma which already exists in the local
SQLite stage; it is not used to invent definitions.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from reader_layers import (
    KnownKeyMatcher,
    iter_lookup_candidates,
    matcher_from_stardict,
    read_book_text,
    scan_coverage,
    write_json,
)


ANALYSIS_VERSION = "frequency-oov-v1"
ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
CAPITALIZED_CYRILLIC_RE = re.compile(r"^[А-ЯЁ][а-яё-]+$")
ALL_CAPS_RE = re.compile(r"^[A-ZА-ЯЁ]{2,10}$")
REPEATED_CHAR_RE = re.compile(r"^(.)\1{3,}$", re.IGNORECASE)
STYLISED_HYPHEN_RE = re.compile(r"^(?:[^-]+-){2,}[^-]+$")

# A closed list keeps lower-case abbreviations conservative. A random short
# Russian word is never labelled an abbreviation merely because it is short.
LOWERCASE_ABBREVIATIONS = frozenset({
    "авт", "акад", "арх", "библ", "бл", "букв", "вв", "гг", "гл", "госп",
    "гр", "губ", "диал", "доц", "др", "ед", "журн", "зам", "изд", "им",
    "иностр", "искл", "ист", "итд", "итп", "кв", "кг", "км", "кол", "коп",
    "край", "лат", "лит", "мл", "млн", "млрд", "м", "мес", "мин", "мн",
    "напр", "науч", "обл", "ок", "пер", "пл", "пос", "пр", "прим", "проф",
    "разг", "ред", "рис", "род", "руб", "св", "см", "ср", "ст", "стр", "сущ",
    "тд", "тел", "тер", "тп", "тыс", "тт", "ул", "уст", "фр", "чел", "шт",
    "экз", "яз",
})


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
    if conn is None or not lemma:
        return ""
    try:
        row = conn.execute(
            "SELECT definition FROM senses WHERE lemma = ? ORDER BY seq LIMIT 1", (lemma,)
        ).fetchone()
    except sqlite3.Error:
        return ""
    return _clean_label(row[0]) if row and row[0] else ""


def _parse_morphology(token: str, morph: Any) -> dict[str, object]:
    """Return only stable morphology details; an unknown parse is harmless."""
    if morph is None:
        return {}
    try:
        parses = morph.parse(token)
        if not parses:
            return {}
        parsed = parses[0]
        lemma = _clean_label(getattr(parsed, "normal_form", "")).casefold()
        tag = getattr(parsed, "tag", "")
        pos = _clean_label(getattr(tag, "POS", ""))
        raw_known = getattr(parsed, "is_known", False)
        known = bool(raw_known() if callable(raw_known) else raw_known)
        return {"lemma": lemma, "part_of_speech": pos, "morph_known": known}
    except Exception:
        return {}


def _all_variants_capitalized(variants: Iterable[str]) -> bool:
    values = [item for item in variants if item]
    return bool(values) and all(CAPITALIZED_CYRILLIC_RE.fullmatch(item) for item in values)


def _is_stylised_hyphenation(token: str) -> bool:
    """Detect only clearly split/stretched spellings such as ``кру-у-гом``."""
    if not STYLISED_HYPHEN_RE.fullmatch(token):
        return False
    parts = token.split("-")
    return len(parts) >= 3 and any(len(part) == 1 for part in parts)


def classify_oov(
    token: str,
    normalized: str,
    variants: Sequence[str],
    script: str,
    morph: Any = None,
    core: sqlite3.Connection | None = None,
) -> dict[str, object]:
    """Classify one OOV surface conservatively without changing coverage.

    ``category`` is a triage label, not a claim that a token is definitely an
    error, a name, or a dictionary headword. ``action`` tells a reviewer where
    the row belongs if it is worth acting on.
    """
    roman = _roman_value(token)
    if roman is not None:
        return {
            "category": "number", "action": "ignore_numeric", "candidate": False,
            "candidate_key": f"roman:{roman}", "lemma": "", "reason": "roman_numeral",
            "confidence": "high",
        }

    if REPEATED_CHAR_RE.fullmatch(normalized) or _is_stylised_hyphenation(normalized):
        return {
            "category": "noise", "action": "ignore_typographic_variant", "candidate": False,
            "candidate_key": normalized, "lemma": "", "reason": "stretched_or_fragmented_spelling",
            "confidence": "high",
        }

    if ALL_CAPS_RE.fullmatch(token) or normalized in LOWERCASE_ABBREVIATIONS:
        return {
            "category": "abbreviation", "action": "review_abbreviation_pack", "candidate": True,
            "candidate_key": normalized, "lemma": "", "reason": "all_caps_or_closed_abbreviation_list",
            "confidence": "high" if ALL_CAPS_RE.fullmatch(token) else "medium",
        }

    if script in {"latin", "greek", "other-alphabet", "mixed"}:
        return {
            "category": "foreign", "action": "review_separate_language_layer", "candidate": True,
            "candidate_key": normalized, "lemma": "", "reason": f"{script}_script",
            "confidence": "high",
        }

    morph_info = _parse_morphology(token, morph) if script == "cyrillic" else {}
    lemma = _clean_label(morph_info.get("lemma", ""))
    known_word = bool(morph_info.get("morph_known", False))
    part_of_speech = _clean_label(morph_info.get("part_of_speech", ""))

    # A capitalised token is merely a *possible* name: a rare ordinary word at
    # sentence start must not be presented as a fact.
    if (
        script == "cyrillic" and _all_variants_capitalized(variants)
        and not known_word and len(normalized) >= 3
    ):
        return {
            "category": "name", "action": "review_literary_name_layer", "candidate": True,
            "candidate_key": normalized, "lemma": "", "reason": "capitalized_only_cyrillic_surface",
            "confidence": "possible",
        }

    if lemma:
        definition = _core_definition(core, lemma)
        if definition and lemma != normalized:
            return {
                "category": "word", "action": "review_inflection_alias", "candidate": True,
                "candidate_key": lemma, "lemma": lemma,
                "reason": "morphology_points_to_existing_core_lemma",
                "confidence": "high" if known_word else "medium", "part_of_speech": part_of_speech,
            }
        return {
            "category": "word", "action": "review_lexicographic_entry", "candidate": True,
            "candidate_key": lemma, "lemma": lemma,
            "reason": "morphological_lemma" if known_word else "guessed_morphological_lemma",
            "confidence": "medium" if known_word else "possible", "part_of_speech": part_of_speech,
        }

    return {
        "category": "word", "action": "review_lexicographic_entry", "candidate": True,
        "candidate_key": normalized, "lemma": "", "reason": "unresolved_cyrillic_word",
        "confidence": "possible",
    }


def _per_file_unknown_counts(
    texts: Sequence[tuple[str, str]], matcher: KnownKeyMatcher
) -> dict[str, Counter[str]]:
    """Map every normalised OOV form to per-book frequency."""
    per_form: dict[str, Counter[str]] = defaultdict(Counter)
    for label, text in texts:
        file_report = scan_coverage([(label, text)], matcher)
        for row in file_report["unknown"]:
            per_form[str(row["normalized"])][label] += int(row["count"])
    return per_form


def _analyse_unknown_rows(
    rows: Sequence[Mapping[str, object]],
    total_unknown: int,
    per_file: Mapping[str, Counter[str]],
    morph: Any,
    core: sqlite3.Connection | None,
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, dict[str, int]]]:
    """Return classified rows, grouped candidates, and unadjusted totals."""
    classified: list[dict[str, object]] = []
    grouped: dict[tuple[str, str, str], dict[str, object]] = {}
    category_summary: dict[str, dict[str, int]] = defaultdict(lambda: {"rows": 0, "tokens": 0})

    for row in rows:
        token = _clean_label(row["token"])
        normalized = _clean_label(row["normalized"])
        variants = [_clean_label(item) for item in row.get("variants", [])]
        count = int(row["count"])
        script = _clean_label(row["script"])
        verdict = classify_oov(token, normalized, variants, script, morph, core)
        category = str(verdict["category"])
        action = str(verdict["action"])
        candidate_key = _clean_label(verdict["candidate_key"])
        sources = per_file.get(normalized, Counter())
        classified_row = {
            "token": token, "normalized": normalized, "count": count,
            "share_percent": round((count / total_unknown) * 100, 4) if total_unknown else 0.0,
            "script": script, "variants": variants,
            **verdict,
        }
        classified.append(classified_row)
        category_summary[category]["rows"] += 1
        category_summary[category]["tokens"] += count

        if not verdict["candidate"]:
            continue
        key = (category, action, candidate_key)
        candidate = grouped.get(key)
        if candidate is None:
            candidate = {
                "category": category, "action": action, "candidate": candidate_key,
                "lemma": _clean_label(verdict.get("lemma", "")), "script": script,
                "reason": _clean_label(verdict["reason"]), "confidence": _clean_label(verdict["confidence"]),
                "part_of_speech": _clean_label(verdict.get("part_of_speech", "")),
                "count": 0, "forms": [], "source_counts": Counter(),
            }
            grouped[key] = candidate
        candidate["count"] = int(candidate["count"]) + count
        candidate["forms"].append({
            "token": token, "normalized": normalized, "count": count, "variants": variants,
        })
        candidate["source_counts"].update(sources)

    candidates: list[dict[str, object]] = []
    for candidate in grouped.values():
        count = int(candidate["count"])
        forms = sorted(candidate.pop("forms"), key=lambda item: (-int(item["count"]), str(item["normalized"])))
        source_counts: Counter[str] = candidate.pop("source_counts")
        candidate["forms"] = forms
        candidate["source_files"] = [
                {"file": Path(label).name, "count": n}
                for label, n in sorted(source_counts.items(), key=lambda item: (-item[1], item[0]))
            ]
        candidate["share_percent"] = round((count / total_unknown) * 100, 4) if total_unknown else 0.0
        candidates.append(candidate)

    candidates.sort(key=lambda item: (-int(item["count"]), str(item["candidate"]), str(item["category"])))
    for rank, candidate in enumerate(candidates, 1):
        candidate["rank"] = rank
    classified.sort(key=lambda item: (-int(item["count"]), str(item["normalized"])))
    return classified, candidates, dict(sorted(category_summary.items()))


def write_candidate_tsv(path: Path, candidates: Sequence[Mapping[str, object]]) -> None:
    """Write reviewable candidates only; never a builder-input TSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow((
            "rank", "category", "action", "candidate", "count", "share_percent",
            "lemma", "forms", "script", "confidence", "reason", "source_files",
        ))
        for row in candidates:
            forms = "; ".join(f"{item['token']} ({item['count']})" for item in row["forms"])
            sources = "; ".join(
                f"{Path(str(item['file'])).name} ({item['count']})" for item in row["source_files"]
            )
            writer.writerow((
                row["rank"], row["category"], row["action"], row["candidate"], row["count"],
                row["share_percent"], row["lemma"], forms, row["script"], row["confidence"],
                row["reason"], sources,
            ))


def analyse_coverage(
    inputs: list[Path],
    index: Path,
    output_tsv: Path,
    report_path: Path,
    core_db: Path | None = None,
    *,
    morph: Any = None,
) -> dict[str, object]:
    """Analyse OOV frequency without modifying any dictionary artefact."""
    texts = [(str(path), read_book_text(path)) for path in inputs]
    observed = {
        candidate
        for _label, text in texts
        for candidate in iter_lookup_candidates(text)
    }
    matcher = matcher_from_stardict(index, observed)
    report = scan_coverage(texts, matcher)
    active_morph = _load_morphology() if morph is None else morph
    conn = sqlite3.connect(f"file:{core_db}?mode=ro", uri=True) if core_db else None
    try:
        per_file = _per_file_unknown_counts(texts, matcher)
        classified, candidates, category_summary = _analyse_unknown_rows(
            report["unknown"], int(report["unknown_tokens"]), per_file, active_morph, conn
        )
    finally:
        if conn is not None:
            conn.close()

    write_candidate_tsv(output_tsv, candidates)
    candidate_tokens = sum(int(item["count"]) for item in candidates)
    report["analysis"] = {
        "version": ANALYSIS_VERSION,
        "metric_contract": {
            "coverage_is_raw": True,
            "coverage_source": "reader_layers.scan_coverage",
            "candidate_rows_do_not_change_coverage": True,
            "synthetic_fallback_definitions_written": False,
        },
        "normalization": {
            "lookup_normalization": "reader_layers.normalize_lookup (NFC, casefold, dash, ё/е)",
            "morphology_available": active_morph is not None,
            "core_lemma_lookup_enabled": core_db is not None,
        },
        "unknown_rows": len(report["unknown"]),
        "unknown_tokens": int(report["unknown_tokens"]),
        "candidate_rows": len(candidates),
        "candidate_tokens": candidate_tokens,
        "category_summary": category_summary,
        "classified_unknown": classified,
        "candidates": candidates,
        "candidate_tsv": str(output_tsv),
    }
    write_json(report_path, report)
    return report


# Kept for old internal callers. Unlike the retired implementation it creates
# no pack and makes no claim of post-analysis 100% coverage.
generate_pack = analyse_coverage


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="EPUB/FB2/TXT books to audit")
    parser.add_argument("--index", required=True, type=Path, help="Existing StarDict .idx")
    parser.add_argument("--core-db", type=Path, help="Optional resolved SQLite stage cache")
    parser.add_argument(
        "--output-tsv", "--candidates-tsv", dest="output_tsv", type=Path,
        default=Path("BOOK_COVERAGE_CANDIDATES.tsv"),
        help="Human review TSV of grouped OOV candidates; it is not a dictionary pack",
    )
    parser.add_argument("--report", type=Path, default=Path("BOOK_COVERAGE_INTERNAL.json"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = analyse_coverage(args.inputs, args.index, args.output_tsv, args.report, args.core_db)
    analysis = report["analysis"]
    print(
        f"[COVERAGE] raw={report['coverage_percent']}%, "
        f"unknown_tokens={report['unknown_tokens']}, "
        f"candidates={analysis['candidate_rows']} -> {analysis['candidate_tsv']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
