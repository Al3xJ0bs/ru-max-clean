#!/usr/bin/env python3
"""Self-contained checks for honest internal OOV frequency analysis."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from internal_book_coverage import analyse_coverage


class _Tag:
    POS = "VERB"


class _Parse:
    def __init__(self, lemma: str, known: bool = True) -> None:
        self.normal_form = lemma
        self.is_known = known
        self.tag = _Tag()


class _Morph:
    def parse(self, token: str):
        if token.casefold() in {"говорили", "говорила"}:
            return [_Parse("говорить")]
        return []


class BookCoverageTests(unittest.TestCase):
    def test_frequency_groups_and_never_changes_raw_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book = root / "fixture.txt"
            book.write_text(
                "говорили говорила Геральт Геральт NASA изд IV кру-у-гом bonjour bonjour",
                encoding="utf-8",
            )
            index = root / "fixture.idx"
            index.write_bytes(b"")  # Valid empty StarDict index: all words are OOV.
            db = root / "stage.sqlite3"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE senses (lemma TEXT, definition TEXT, seq INTEGER)")
            conn.execute("INSERT INTO senses VALUES ('говорить', 'Пользоваться устной речью.', 1)")
            conn.commit()
            conn.close()
            tsv = root / "candidates.tsv"
            report_path = root / "report.json"

            report = analyse_coverage([book], index, tsv, report_path, db, morph=_Morph())
            analysis = report["analysis"]

            self.assertLess(float(report["coverage_percent"]), 100.0)
            self.assertEqual(report["unknown_tokens"], 10)
            self.assertTrue(analysis["metric_contract"]["coverage_is_raw"])
            self.assertTrue(analysis["metric_contract"]["candidate_rows_do_not_change_coverage"])
            self.assertFalse(analysis["metric_contract"]["synthetic_fallback_definitions_written"])
            self.assertTrue(analysis["normalization"]["morphology_available"])
            self.assertTrue(analysis["normalization"]["core_lemma_lookup_enabled"])
            self.assertNotIn("coverage_after", report)
            self.assertNotIn("generated_pack", report)

            candidates = {item["candidate"]: item for item in analysis["candidates"]}
            self.assertEqual(candidates["говорить"]["count"], 2)
            self.assertEqual(candidates["говорить"]["action"], "review_inflection_alias")
            self.assertEqual(candidates["геральт"]["category"], "name")
            self.assertEqual(candidates["nasa"]["category"], "abbreviation")
            self.assertEqual(candidates["изд"]["category"], "abbreviation")
            self.assertEqual(candidates["bonjour"]["category"], "foreign")
            self.assertEqual(candidates["bonjour"]["count"], 2)
            self.assertNotIn("roman:4", candidates)
            self.assertNotIn("кру-у-гом", candidates)

            classified = {item["normalized"]: item for item in analysis["classified_unknown"]}
            self.assertEqual(classified["iv"]["category"], "number")
            self.assertEqual(classified["кру-у-гом"]["category"], "noise")
            self.assertEqual(analysis["category_summary"]["number"]["tokens"], 1)
            self.assertEqual(analysis["category_summary"]["noise"]["tokens"], 1)

            tsv_text = tsv.read_text(encoding="utf-8")
            self.assertIn("review_inflection_alias", tsv_text)
            self.assertNotIn("roman:4", tsv_text)
            self.assertNotIn("кру-у-гом", tsv_text)
            persisted = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["coverage_percent"], report["coverage_percent"])

    def test_parser_keeps_legacy_output_option_as_candidate_report(self) -> None:
        from internal_book_coverage import parse_args

        args = parse_args(["book.txt", "--index", "dict.idx", "--output-tsv", "out.tsv"])
        self.assertEqual(args.output_tsv, Path("out.tsv"))
        args = parse_args(["book.txt", "--index", "dict.idx", "--candidates-tsv", "out.tsv"])
        self.assertEqual(args.output_tsv, Path("out.tsv"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
