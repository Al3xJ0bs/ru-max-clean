#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import struct
import tempfile
import unittest

from build_reader_packs import build_pack, pack_title
from reader_layers import (
    KnownKeyMatcher,
    iter_stardict_keys,
    normalize_lookup,
    read_book_text,
    scan_coverage,
    script_of,
)
from reader_pack_loader import load_pack_tsv


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
        a_propos = next(entry for entry in french if entry.word == "à propos")
        self.assertIn("a propos", a_propos.aliases)
        s_il_vous_plait = next(entry for entry in french if entry.word == "s'il vous plaît")
        self.assertIn("s’il vous plait", s_il_vous_plait.aliases)
        names = load_pack_tsv(Path(__file__).with_name("reader_packs") / "literary_names.tsv")
        self.assertGreaterEqual(len(names), 90)
        self.assertTrue(any(entry.word == "Фродо" and "Фродо" in entry.aliases for entry in names))
        self.assertTrue(any(entry.word == "Кламм" and "Кламма" in entry.aliases for entry in names))
        self.assertTrue(any(entry.word == "Санчо" and "Панса" in entry.aliases for entry in names))
        self.assertTrue(any(entry.word == "Хельсинг" and "Ван Хельсинг" in entry.aliases for entry in names))
        self.assertTrue(any(entry.word == "Вёшенская" and "Вешенской" in entry.aliases for entry in names))
        names_by_word = {entry.word: entry for entry in names}
        self.assertIn("Соборяне", names_by_word["Термосесов"].definition)
        self.assertIn("Ротгера", names_by_word["Ротгер"].aliases)
        self.assertIn("Сьюарда", names_by_word["Сьюард"].aliases)
        self.assertIn("Эстеллы", names_by_word["Эстелла"].aliases)
        self.assertIn("Исилдура", names_by_word["Исилдур"].aliases)

        selected_name_keys = [item for entry in names for item in (entry.word, *entry.aliases)]
        report = scan_coverage(
            [("observed-forms", "Термосесов Ротгера Сьюарда Эстеллы Исилдура")],
            KnownKeyMatcher(selected_name_keys),
        )
        self.assertEqual(report["unknown_tokens"], 0)

    def test_literary_abbreviations_pack(self) -> None:
        entries = load_pack_tsv(
            Path(__file__).with_name("reader_packs") / "literary_abbreviations.tsv"
        )
        words = {entry.word for entry in entries}
        self.assertIn("г-жа", words)
        self.assertIn("стр", words)
        self.assertIn("проч", words)
        self.assertTrue(any(entry.word == "г-н" and "г-ном" in entry.aliases for entry in entries))
        fantasy = load_pack_tsv(Path(__file__).with_name("reader_packs") / "fantasy_terms.tsv")
        self.assertGreaterEqual(len(fantasy), 15)
        self.assertTrue(any(entry.word == "палантир" for entry in fantasy))
        self.assertTrue(any(entry.word == "энт" and "онты" in entry.aliases for entry in fantasy))
        ent = next(entry for entry in fantasy if entry.word == "энт")
        self.assertTrue({"онта", "онтам", "онтов", "онтами"}.issubset(ent.aliases))
        ent_keys = [item for item in (ent.word, *ent.aliases)]
        self.assertEqual(
            scan_coverage([("observed-forms", "онта онтам онтов онтами")], KnownKeyMatcher(ent_keys))[
                "unknown_tokens"
            ],
            0,
        )
        literary_terms = load_pack_tsv(Path(__file__).with_name("reader_packs") / "literary_terms.tsv")
        self.assertGreaterEqual(len(literary_terms), 5)
        self.assertTrue(any(entry.word == "фельдкурат" for entry in literary_terms))

    def test_foreign_phrase_typography_and_german_layer(self) -> None:
        pack_dir = Path(__file__).with_name("reader_packs")
        french = load_pack_tsv(pack_dir / "french_literary.tsv")
        french_keys = [item for entry in french for item in (entry.word, *entry.aliases)]
        report = scan_coverage(
            [("demo", "s\u2019il vous plait, à propos.")],
            KnownKeyMatcher(french_keys),
        )
        self.assertEqual(report["unknown_tokens"], 0)
        german = load_pack_tsv(pack_dir / "german_literary.tsv")
        self.assertGreaterEqual(len(german), 20)
        self.assertTrue(any(entry.word == "Halt" and "хальт" in entry.aliases for entry in german))
        self.assertTrue(any(entry.word == "gute Nacht" for entry in german))

    def test_built_pack_keeps_typographic_phrase_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = build_pack(
                Path(__file__).with_name("reader_packs") / "french_literary.tsv",
                Path(tmp),
            )
            self.assertEqual(result["title"], "Французская лексика в литературе")
            index = Path(tmp) / "french_literary" / "ru-max-clean.idx"
            keys = set(iter_stardict_keys(index))
            self.assertIn("a propos", keys)
            self.assertIn("s\u2019il vous plait", keys)

    def test_built_german_pack_has_separate_reader_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = build_pack(
                Path(__file__).with_name("reader_packs") / "german_literary.tsv",
                Path(tmp),
            )
            self.assertEqual(result["language"], "de")
            self.assertEqual(result["title"], "Немецкая лексика в литературе")
            index = Path(tmp) / "german_literary" / "ru-max-clean.idx"
            self.assertIn("Fraulein", set(iter_stardict_keys(index)))

    def test_pack_titles_are_reader_friendly(self) -> None:
        self.assertEqual(pack_title("latin_wiktionary"), "Латынь — расширенный словарь")
        self.assertEqual(pack_title("literary_abbreviations"), "Литературные сокращения")
        self.assertEqual(pack_title("german_literary"), "Немецкая лексика в литературе")
        self.assertEqual(pack_title("unknown_custom_layer"), "Дополнительный словарь — unknown_custom_layer")

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
