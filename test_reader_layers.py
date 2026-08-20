#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import struct
import tempfile
import unittest

from reader_layers import (
    KnownKeyMatcher,
    iter_stardict_keys,
    load_pack_tsv,
    normalize_lookup,
    read_book_text,
    scan_coverage,
    script_of,
)


class ReaderLayerTests(unittest.TestCase):
    def test_normalization_and_scripts(self) -> None:
        self.assertEqual(normalize_lookup("  Статус–Кво  "), "статус-кво")
        self.assertEqual(script_of("латынь"), "cyrillic")
        self.assertEqual(script_of("a priori"), "latin")
        self.assertEqual(script_of("Q-слово"), "mixed")

    def test_phrase_coverage_does_not_report_components(self) -> None:
        report = scan_coverage(
            [("demo", "Статус-кво и a priori; неизвестное слово")],
            KnownKeyMatcher(["статус-кво", "a priori"]),
        )
        self.assertEqual(report["known_tokens"], 3)
        self.assertEqual(report["unknown_tokens"], 3)
        unknown = {row["normalized"] for row in report["unknown"]}
        self.assertNotIn("a", unknown)
        self.assertNotIn("priori", unknown)
        self.assertIn("неизвестное", unknown)

    def test_phrase_and_single_word_hits_are_not_double_counted(self) -> None:
        report = scan_coverage(
            [("demo", "alpha beta alpha")],
            KnownKeyMatcher(["alpha", "alpha beta"]),
        )
        self.assertEqual(report["tokens_total"], 3)
        self.assertEqual(report["known_tokens"], 3)
        self.assertEqual(report["unknown_tokens"], 0)
        self.assertEqual(report["coverage_percent"], 100.0)

    def test_pack_loader(self) -> None:
        path = Path(__file__).with_name("reader_packs") / "latin_classical.tsv"
        entries = load_pack_tsv(path)
        self.assertGreaterEqual(len(entries), 40)
        self.assertTrue(any("статус-кво" in entry.aliases for entry in entries))
        french = load_pack_tsv(Path(__file__).with_name("reader_packs") / "french_literary.tsv")
        self.assertGreaterEqual(len(french), 70)
        self.assertTrue(any(entry.word == "monsieur" and "месье" in entry.aliases for entry in french))
        names = load_pack_tsv(Path(__file__).with_name("reader_packs") / "literary_names.tsv")
        self.assertGreaterEqual(len(names), 90)
        self.assertTrue(any(entry.word == "Фродо" and "Фродо" in entry.aliases for entry in names))
        fantasy = load_pack_tsv(Path(__file__).with_name("reader_packs") / "fantasy_terms.tsv")
        self.assertGreaterEqual(len(fantasy), 15)
        self.assertTrue(any(entry.word == "палантир" for entry in fantasy))
        literary_terms = load_pack_tsv(Path(__file__).with_name("reader_packs") / "literary_terms.tsv")
        self.assertGreaterEqual(len(literary_terms), 5)
        self.assertTrue(any(entry.word == "фельдкурат" for entry in literary_terms))

    def test_stardict_index_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.idx"
            with path.open("wb") as stream:
                stream.write(b"alpha\0" + struct.pack(">II", 0, 3))
                stream.write("статус-кво".encode() + b"\0" + struct.pack(">II", 3, 4))
            self.assertEqual(list(iter_stardict_keys(path)), ["alpha", "статус-кво"])

    def test_fb2_binary_and_metadata_are_not_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "book.fb2"
            path.write_text(
                "<FictionBook><description><title-info>Автор</title-info></description>"
                "<binary id='cover'>wA Pj4 base64 payload</binary>"
                "<body><section><p>Геральт status quo</p></section></body></FictionBook>",
                encoding="utf-8",
            )
            text = read_book_text(path)
            self.assertIn("Геральт", text)
            self.assertNotIn("base64", text)
            self.assertNotIn("Автор", text)
            self.assertNotIn("<p>", text)
            self.assertNotIn("</p>", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
