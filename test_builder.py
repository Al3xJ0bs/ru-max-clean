#!/usr/bin/env python3
import shutil
import json
import os
import sqlite3
import struct
import subprocess
import sys
import tarfile
import zlib
from pathlib import Path

from build_ru_max_clean import _rapidgzip_enabled, is_lookup_key

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "_test_output"
LEGACY = ROOT / "_test_dal.tar.bz2"

assert is_lookup_key("è")
assert is_lookup_key("à")
assert is_lookup_key("Brüder")

# rapidgzip must never be selected merely because a wheel happens to be
# installed.  This protects Windows users from the known slow readline path.
_rapidgzip_flag = os.environ.pop("RU_MAX_ENABLE_RAPIDGZIP", None)
try:
    assert not _rapidgzip_enabled()
finally:
    if _rapidgzip_flag is not None:
        os.environ["RU_MAX_ENABLE_RAPIDGZIP"] = _rapidgzip_flag


def lookup(base: Path, word: str):
    idx = base.with_suffix(".idx").read_bytes()
    dic = base.with_suffix(".dict").read_bytes()
    pos = 0
    while pos < len(idx):
        z = idx.find(b"\0", pos)
        key = idx[pos:z].decode("utf-8")
        off, size = struct.unpack(">II", idx[z + 1:z + 9])
        pos = z + 9
        if key == word:
            return off, size, dic[off:off + size].decode("utf-8")
    raise KeyError(word)


def make_legacy_dal_archive(path: Path) -> None:
    """Create a tiny StarDict archive for importer/fallback regression tests."""
    tmp = ROOT / "_legacy_fixture"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    stem = tmp / "dal-test"

    entries = [
        ("десница ж", "десница, ж. стар. Правая рука. || Длинный пример, который не должен попасть в карточку."),
        ("ключ", "ключ, м. Неверное резервное толкование, которое не должно заменить Викисловарь."),
    ]
    dict_bytes = bytearray()
    idx_bytes = bytearray()
    for word, article in entries:
        body = article.encode("utf-8")
        off = len(dict_bytes)
        dict_bytes.extend(body)
        idx_bytes.extend(word.encode("utf-8") + b"\0" + struct.pack(">II", off, len(body)))

    syn_bytes = "десницею".encode("utf-8") + b"\0" + struct.pack(">I", 0)
    (stem.with_suffix(".dict")).write_bytes(dict_bytes)
    (stem.with_suffix(".idx")).write_bytes(idx_bytes)
    (stem.with_suffix(".syn")).write_bytes(syn_bytes)
    (stem.with_suffix(".ifo")).write_text(
        "StarDict's dict ifo file\n"
        "version=2.4.2\n"
        "wordcount=2\n"
        f"idxfilesize={len(idx_bytes)}\n"
        "synwordcount=1\n"
        "bookname=Dal test\n"
        "sametypesequence=m\n",
        encoding="utf-8",
    )
    with tarfile.open(path, "w:bz2") as tf:
        for ext in (".ifo", ".idx", ".dict", ".syn"):
            p = stem.with_suffix(ext)
            tf.add(p, arcname="dal-test/" + p.name)
    shutil.rmtree(tmp)


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    if LEGACY.exists():
        LEGACY.unlink()
    make_legacy_dal_archive(LEGACY)
    subprocess.run([sys.executable, str(ROOT / "make_demo_sources.py")], check=True)
    subprocess.run([
        sys.executable, str(ROOT / "build_ru_max_clean.py"),
        "--kaikki", str(ROOT / "sample_raw.jsonl.gz"),
        "--wikidata-lexemes", str(ROOT / "sample_wikidata_lexemes.json.bz2"),
        "--dal", str(LEGACY),
        "--wikipedia", str(ROOT / "sample_ruwiki.xml.bz2"),
        "--wikipedia-quality-upgrade",
        "--extra-tsv", str(ROOT / "extra_terms.example.tsv"),
        "--extra-jsonl", str(ROOT / "extra_terms.example.jsonl"),
        "--output-dir", str(OUT),
    ], check=True)
    base = OUT / "ru-max-clean"
    stats_payload = json.loads((OUT / "BUILD_STATS.json").read_text(encoding="utf-8"))
    removed_source_marker = "open" + "corpora"
    assert removed_source_marker not in json.dumps(stats_payload, ensure_ascii=False).casefold()
    assert removed_source_marker not in (OUT / "SOURCES.txt").read_text(encoding="utf-8").casefold()
    build_info = json.loads((OUT / "BUILD_INFO.json").read_text(encoding="utf-8"))
    assert removed_source_marker not in json.dumps(build_info, ensure_ascii=False).casefold()
    subprocess.run([sys.executable, str(ROOT / "validate_stardict.py"), str(base)], check=True)

    k = lookup(base, "ключ")
    kf = lookup(base, "ключами")
    assert k[:2] == kf[:2], "single-lemma forms must reuse the same article bytes"
    assert "падеж" not in kf[2].casefold()
    assert "неверное резервное" not in k[2].casefold(), "Dal fallback must not replace Wiktionary"
    assert lookup(base, "елка")[:2] == lookup(base, "ёлка")[:2]
    assert "полупроводник" in lookup(base, "полупроводниковому")[2]
    steel = lookup(base, "стали")[2]
    assert steel.startswith("1. ") and "\n2. " in steel
    assert "падеж" not in steel.casefold()
    assert lookup(base, "рѣчь")[2] == "Способность говорить; словесное выражение мыслей."
    assert "кислотность" in lookup(base, "pH")[2]
    assert "управления" in lookup(base, "PID-регулятор")[2]

    # Wikidata Lexemes: Russian glosses add missing modern terms and forms become
    # lookup aliases. Existing Wiktionary meanings keep priority.
    eut = lookup(base, "эвтектика")
    assert "температуру плавления" in eut[2]
    assert lookup(base, "эвтектикой")[:2] == eut[:2]
    assert lookup(base, "ключиком")[:2] == k[:2]
    assert "Резервное значение" not in k[2]

    # Wikipedia terminology is last-resort only, restricted to professional/scientific
    # categories, and strips the repeated headword. Parenthesized specialist pages
    # can provide a base-title alias only if the base word is otherwise undefined.
    cryo = lookup(base, "криогенная техника")[2]
    assert cryo.startswith("Совокупность методов и устройств")
    assert "Криогенная техника —" not in cryo
    shunt = lookup(base, "шунт")[2]
    assert "электрический проводник" in shunt.casefold()
    assert "специальный идентификатор" not in k[2].casefold(), "Wikipedia must not override/duplicate an existing base lemma"
    try:
        lookup(base, "Иванов, Иван Иванович")
    except KeyError:
        pass
    else:
        raise AssertionError("biographical Wikipedia pages must not enter the dictionary")

    # Dal fallback adds a previously missing old word, strips headword/POS/register
    # metadata and drops the example tail after ||. Synonyms reuse the same bytes.
    right_hand = lookup(base, "десница")
    assert right_hand[2] == "Правая рука."
    assert lookup(base, "десницею")[:2] == right_hand[:2]

    # Edge-hyphen morphemes are not standalone dictionary words.
    try:
        lookup(base, "-у")
    except KeyError:
        pass
    else:
        raise AssertionError("affix-only key '-у' must not be indexed")

    # Grammatical terminology is allowed when it is the actual lexical meaning.
    assert lookup(base, "датив")[2] == "Дательный падеж."

    # Regression from a full 2026-08 Russian Wiktionary build: a lexical term may
    # legitimately start with "Форма ...". It is not a form-of redirect unless it
    # actually points to another word/lexeme.
    from build_ru_max_clean import clean_definition
    comparative = (
        "Форма прилагательного и наречия или синтаксическая конструкция, "
        "обозначающая большую степень проявления признака по сравнению с другой."
    )
    assert clean_definition(comparative) == comparative
    # v3.1 regression: normal definitions may begin with "Форма" and contain
    # the preposition "от" later in the sentence. They are lexical meanings.
    state_power = (
        "Форма политической власти, осуществляемая от имени и в интересах народа "
        "государством в пределах своей территории."
    )
    dependency = "Форма зависимости одной величины от другой."
    assert clean_definition(state_power) == state_power
    assert clean_definition(dependency) == dependency
    assert clean_definition("Форма глагола от слова идти") == ""
    assert clean_definition("Форма глагола идти") == ""
    assert clean_definition("Родительный падеж слова дом") == ""
    assert clean_definition("Множественное число слова дом") == ""
    # Angle brackets can be linguistic notation, not HTML. This exact pattern
    # occurs in Russian Wiktionary (e.g. иканье: <и> — <э>).
    angle = lookup(base, "и́каний")[2]
    assert "<и>" in angle and "<э>" in angle

    assert clean_definition("<b>Жирный текст</b>") == "Жирный текст"
    assert clean_definition("Неразличение <и> — <э> в безударных слогах.") == "Неразличение <и> — <э> в безударных слогах."
    # Real Kindle screenshot exposed this: register/domain labels must not consume
    # popup space when the user asked for meanings only.
    assert clean_definition("истор. старинное женское украшение в виде головной повязки") == "Старинное женское украшение в виде головной повязки"
    assert clean_definition("(физ.) зависимость физических свойств от направления") == "Зависимость физических свойств от направления"
    assert clean_definition("устар., книжн. старое значение") == "Старое значение"
    assert clean_definition("Исторический процесс развития общества.") == "Исторический процесс развития общества."

    # Kindle screenshot regressions: abbreviated participle/form metadata must not
    # appear as a meaning. If it points to a lemma, the lookup becomes an alias.
    buried = lookup(base, "вкопанный")
    bury = lookup(base, "вкопать")
    assert buried[2] == "Помещённый и укреплённый внутри вырытого углубления."
    assert buried[:2] != bury[:2], "display override should have its own shared article pointer"
    assert "страд." not in buried[2].casefold() and "прич." not in buried[2].casefold()
    lit = lookup(base, "освещенному")[2]
    assert lit == "Такой, где есть освещение"
    assert "страд." not in lit.casefold() and "адъектив." not in lit.casefold()
    assert clean_definition("Страд. прич. прош. вр. от вкопать") == ""
    assert clean_definition("Адъектив. такой, где есть освещение") == "Такой, где есть освещение"
    assert clean_definition("(в земельных отношениях) ограниченное право") == "Ограниченное право"
    # 4.3 final scrub regressions from the real 4.2 QUALITY_REPORT.
    assert clean_definition("Действ. прич. наст. вр. от агглютинировать") == ""
    assert clean_definition("Прич. от расшить") == ""
    assert clean_definition("Реки в России, притоки рек Мезень, Вага {{пример|сырой хвост") == "Реки в России, притоки рек Мезень, Вага"

    # 4.5 active semantic pass: target the concrete classes exposed by the full
    # 4.3 QUALITY_REPORT without touching the expensive source parsers.
    from build_ru_max_clean import (
        _quality_normalize_definition, semantic_quality_pass, wikipedia_quality_rescue,
        connect_db, ensure_runtime_indexes, add_sense, add_link, write_quality_report,
        _alias_candidates, _quality_rewrite_about_phrase,
    )
    index_db = ROOT / "_deferred_indexes.sqlite3"
    index_db.unlink(missing_ok=True)
    ic = connect_db(index_db)
    before_indexes = {
        row[0]
        for row in ic.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "senses_lemma_idx" not in before_indexes
    assert "links_lemma_idx" not in before_indexes
    assert "form_hints_target_idx" not in before_indexes
    ensure_runtime_indexes(ic)
    after_indexes = {
        row[0]
        for row in ic.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert {"senses_lemma_idx", "links_lemma_idx", "form_hints_target_idx"} <= after_indexes
    ic.close(); index_db.unlink(missing_ok=True)
    # Stress aliases must preserve Cyrillic breve: ``руко́й`` -> ``рукой``,
    # never the corrupt ``рукои``.
    assert "рукой" in _alias_candidates("руко́й", True, True, True)
    assert "рукои" not in _alias_candidates("руко́й", True, True, True)
    q, changes = _quality_normalize_definition(
        "Ambient Marketing",
        "(от англ. ambient — окружение) направление в рекламе, использующее окружающую среду.",
        "ruwiki-lead",
    )
    assert q == "Направление в рекламе, использующее окружающую среду."
    assert "leading_metadata_removed" in changes
    q, _ = _quality_normalize_definition(
        "Абзу", "(в шумерской мифологии) мировой океан подземных пресных вод, окружающий землю", "wiktionary:ru"
    )
    assert q == "Мировой океан подземных пресных вод, окружающий землю в шумерской мифологии"
    q, changes = _quality_normalize_definition(
        "Бастет", "(в Древнем Египте) имя богини радости и домашнего очага", "wiktionary:ru"
    )
    assert q == "Имя богини радости и домашнего очага в Древнем Египте"
    assert "leading_context_rewritten" in changes and not q.startswith("(")
    q, _ = _quality_normalize_definition(
        "агонистический", "(в этологии) относящийся к активности, связанной с борьбой", "wiktionary:ru"
    )
    assert q == "Относящийся к активности, связанной с борьбой"
    q, _ = _quality_normalize_definition(
        "МОУ", "(в РФ с 2000-х гг.) сокр. от муниципальное образовательное учреждение", "wiktionary:ru"
    )
    assert q == "Муниципальное образовательное учреждение"
    q, _ = _quality_normalize_definition(
        "Рефлекс Гордона",
        "(патологический стопный разгибательный рефлекс) проявляющийся в медленном разгибании I пальца стопы.",
        "ruwiki-lead",
    )
    assert q.startswith("Патологический стопный разгибательный рефлекс, проявляющийся")
    q, _ = _quality_normalize_definition(
        "Он",
        "(Гелиополь, Гелиополис, Илиополь (Ἡλίουπόλις, егип. Иуну, библ. Он)) один из важнейших городов в Древнем Египте",
        "wiktionary:ru",
    )
    assert q == "Один из важнейших городов в Древнем Египте"
    q, _ = _quality_normalize_definition(
        "Растущее",
        "(до 1948 года Ойсунки́) село в Бахчисарайском районе Республики Крым",
        "wiktionary:ru",
    )
    assert q == "Село в Бахчисарайском районе Республики Крым"
    q, _ = _quality_normalize_definition(
        "Fairlight CMI", "(с функциями сэмплера и цифровой звуковой рабочей станции) выпущенный в 1979 году компанией .", "ruwiki-lead"
    )
    assert q == ""
    # Source-aware Wikipedia fixtures: only a completed lead followed by a
    # detached date/history tail is trimmed.  The same text from Wiktionary is
    # left alone, and a malformed exact-title ``(,`` self-reference is rejected
    # without broad parenthetical/about rewrites.
    q, changes = _quality_normalize_definition(
        "Захват Бахрейна",
        "Захват Бахрейна войсками персидского шаха Аббаса I в 1602 году. , 1622 год",
        "ruwiki-lead",
    )
    assert q == "Захват Бахрейна войсками персидского шаха Аббаса I в 1602 году."
    assert "wikipedia_orphan_date_tail_removed" in changes
    q, changes = _quality_normalize_definition(
        "Захват Бахрейна",
        "Захват Бахрейна войсками персидского шаха Аббаса I в 1602 году. , 1622 год",
        "wiktionary:ru",
    )
    assert q.endswith("1622 год") and not changes
    q, changes = _quality_normalize_definition(
        "Национальная пресса (Бразилия)",
        "Национальная пресса в период с 2007 по 2010 год. , историческое издание Национальной прессы 1991 года.",
        "ruwiki-lead",
    )
    assert q == "Национальная пресса в период с 2007 по 2010 год."
    assert "wikipedia_orphan_date_tail_removed" in changes
    q, changes = _quality_normalize_definition(
        "Закон о регистрации",
        "Закон США, принятый в 1938 году .",
        "ruwiki-lead",
    )
    assert q == "Закон США, принятый в 1938 году."
    assert "wikipedia_date_punctuation_fixed" in changes
    q, changes = _quality_normalize_definition(
        "Кирха в Кведнау",
        "Кирха в Кведнау (, ныне называется Северная Гора), районе Кёнигсберга (сейчас — конец улицы Александра Невского в Калининграде).",
        "ruwiki-lead",
    )
    assert q == "" and "wikipedia_self_reference_removed" in changes
    q, changes = _quality_normalize_definition(
        "GiNaC", "GiNaC является C++ библиотекой.", "ruwiki-lead"
    )
    assert q == "GiNaC является C++ библиотекой." and "wikipedia_self_reference_removed" not in changes
    q, changes = _quality_normalize_definition(
        ".460 Steyr", "Крупнокалиберный патрон, разработанный австрийской компанией Steyr в 2002 году.", "ruwiki-lead"
    )
    assert q == "Крупнокалиберный патрон." and "wikipedia_history_tail_removed" in changes
    q, _ = _quality_normalize_definition(
        ".NET Framework", "Программная платформа, выпущенная компанией Microsoft в 2002 году.", "ruwiki-lead"
    )
    assert q == "Программная платформа."
    q, changes = _quality_normalize_definition("Вокульский", "Вокульский (фамилия)", "wiktionary:ru")
    assert q == "Фамилия" and "trailing_descriptor_rewritten" in changes
    q, changes = _quality_normalize_definition(
        "Физический факультет",
        "Физический факультет МГУ * Физический факультет УрГУ * Физический факультет СПбГУ",
        "ruwiki-lead",
    )
    assert q == "" and "wikipedia_list_residue_removed" in changes
    q, _ = _quality_normalize_definition("Вране", "(м.р.) город в Сербии", "wiktionary:ru")
    assert q == "Город в Сербии"
    q, _ = _quality_normalize_definition("4B5B", "Это тип линейного кодирования для передачи данных.", "ruwiki-lead")
    assert q == "Тип линейного кодирования для передачи данных."
    q, changes = _quality_normalize_definition(
        "4B5B",
        "Это тип линейного кодирования для передачи данных. 4B5B отображает группы 4 бит в группы 5 бит.",
        "ruwiki-lead",
    )
    assert q == "Тип линейного кодирования для передачи данных."
    assert "wikipedia_extra_sentences_removed" in changes
    q, changes = _quality_normalize_definition(
        "122-мм гаубица Д-30",
        "Советская буксируемая 122-мм гаубица, принятая на вооружение ВС СССР 12 мая 1960 года.",
        "ruwiki-lead",
    )
    assert q == "Советская буксируемая 122-мм гаубица."
    assert "wikipedia_history_tail_removed" in changes
    # Never create an ungrammatical instrumental fragment by stripping a copula.
    q, changes = _quality_normalize_definition(
        "GiNaC", "GiNaC является C++ библиотекой.", "ruwiki-lead"
    )
    assert q == "GiNaC является C++ библиотекой."
    assert "headword_prefix_removed" not in changes
    q, _ = _quality_normalize_definition(
        "Белый дом",
        "О расстреле Белого дома [3], где заседали депутаты ◆ Белый дом — это пример [НКРЯ]",
        "wiktionary:ru",
    )
    assert q == ""
    q, _ = _quality_normalize_definition(
        "антипасха",
        "Неделя по Пасхе, Фомина неделя, Красная горка. Антипат м. черный коралл. Антипатия ж. природное отвращение.",
        "dal",
    )
    assert q == "Неделя по Пасхе, Фомина неделя, Красная горка."
    q, _ = _quality_normalize_definition(
        "1000 км Алгарве 2010", "Третий раунд сезона 2010 LMS.", "ruwiki-lead"
    )
    assert q == ""
    # Wikipedia date detection must not mistake a version/range token such as
    # ECMAScript 2015+ for an encyclopedic historical date.
    from build_ru_max_clean import _human_years
    assert _human_years("ECMAScript 2015+ и 2016 года") == {"2016"}
    q, changes = _quality_normalize_definition(
        "LMSS",
        "Космическая платформа LMSS (сокр. от ) являющаяся подразделением компании.",
        "ruwiki-lead",
    )
    assert "LMSS, являющаяся" in q
    assert "wikipedia_broken_tail_removed" in changes

    alias_db = ROOT / "_quality_alias.sqlite3"
    alias_db.unlink(missing_ok=True)
    qc = connect_db(alias_db)
    add_sense(qc, "Европа", "Часть света.", "test")
    add_link(qc, "Европа", "Европа")
    add_sense(qc, "Єѵрѡпа", "Европа", "wiktionary:cu")
    add_link(qc, "Єѵрѡпа", "Єѵрѡпа")
    st = semantic_quality_pass(qc)
    assert st["textual_aliases_converted"] == 1
    assert qc.execute("SELECT COUNT(*) FROM senses WHERE lemma='Єѵрѡпа'").fetchone()[0] == 0
    assert qc.execute("SELECT 1 FROM links WHERE key='Єѵрѡпа' AND lemma='Европа'").fetchone()
    qc.close(); alias_db.unlink(missing_ok=True)

    # 4.6 regression: a bare modern one-word meaning is a meaning, not a redirect.
    # 4.5 accidentally collapsed thousands of rows such as "Абаза -> Фамилия".
    modern_alias_db = ROOT / "_quality_modern_alias.sqlite3"
    modern_alias_db.unlink(missing_ok=True)
    mc = connect_db(modern_alias_db)
    add_sense(mc, "Фамилия", "Наследственное семейное именование человека.", "test")
    add_link(mc, "Фамилия", "Фамилия")
    add_sense(mc, "Абаза", "Фамилия", "wiktionary:ru")
    add_link(mc, "Абаза", "Абаза")
    add_sense(mc, "Церковь", "Христианская религиозная организация или храм.", "test")
    add_link(mc, "Церковь", "Церковь")
    add_sense(mc, "Божий дом", "Церковь", "wiktionary:ru")
    add_link(mc, "Божий дом", "Божий дом")
    mst = semantic_quality_pass(mc)
    assert mst["textual_aliases_converted"] == 0
    assert mc.execute("SELECT definition FROM senses WHERE lemma='Абаза'").fetchone()[0] == "Фамилия"
    assert mc.execute("SELECT definition FROM senses WHERE lemma='Божий дом'").fetchone()[0] == "Церковь"
    mc.close(); modern_alias_db.unlink(missing_ok=True)

    # 4.7: textual relation senses are removed even when the same headword has
    # another real meaning.  Morphology-resolvable "О ..." glosses become hidden
    # aliases, while an unresolved long "то же, что" expansion becomes direct
    # semantic text rather than metadata.
    from build_ru_max_clean import (
        _parse_alias_formula,
        definition_quality_flags,
        definition_quality_report_score,
        definition_quality_score,
    )
    # The public report must expose the real heuristic, not a presentation-only
    # confidence score.
    clean_definition = "Органическое вещество, образующееся в клетках растений."
    clean_flags = definition_quality_flags("хлорофилл", clean_definition, "wiktionary:ru")
    clean_raw = definition_quality_score("хлорофилл", clean_definition, "wiktionary:ru", clean_flags)
    assert not clean_flags
    assert clean_raw == definition_quality_report_score(
        "хлорофилл", clean_definition, "wiktionary:ru", clean_flags, _raw_score=clean_raw
    )
    concise_flags = definition_quality_flags("Александр", "мужское имя", "wiktionary:ru")
    assert concise_flags == ["onomastic_stub"]
    assert definition_quality_report_score(
        "Александр", "мужское имя", "wiktionary:ru", concise_flags
    ) == definition_quality_score("Александр", "мужское имя", "wiktionary:ru", concise_flags)
    warning_text = "О доме"
    warning_flags = definition_quality_flags("дом", warning_text, "wiktionary:ru")
    warning_raw = definition_quality_score("дом", warning_text, "wiktionary:ru", warning_flags)
    assert "about_fragment" in warning_flags
    assert definition_quality_report_score(
        "дом", warning_text, "wiktionary:ru", warning_flags, _raw_score=warning_raw
    ) == warning_raw
    # An unflagged but very-low raw score must remain visible in the report.
    assert definition_quality_report_score(
        "термтест", "Женщина)", "wiktionary:ru", [], _raw_score=42
    ) == 42
    assert _parse_alias_formula("Вариант бдеющій") is not None
    assert _parse_alias_formula("Вариант бѹкварь") is not None
    assert _parse_alias_formula("Вариант грѧдꙑ") is not None
    assert _parse_alias_formula("Вариант названия индийского города Мумбаи")[1] == ["Мумбаи"]
    assert _parse_alias_formula("Вариант именования города Бурса")[1] == ["Бурса"]
    assert _parse_alias_formula("Вариант написания города Каликут, расположенного ...")[1] == ["Каликут"]
    assert _parse_alias_formula("Вариант кодирования цифрового кода в виде буквенно-цифрового текста") is None
    assert _parse_alias_formula("Вариант фонемы в слабой позиции") is None
    assert "redirect_residue" not in definition_quality_flags(
        "Base58", "Вариант кодирования цифрового кода в виде буквенно-цифрового текста", "ruwiki-lead"
    )
    assert "url_residue" not in definition_quality_flags(
        "VRML", "Стандартизированный формат файлов для трёхмерной графики, используется в WWW.", "ruwiki-lead"
    )
    q, changes = _quality_normalize_definition(
        "антитиреоидный",
        "(о лекарственных препаратах) тормозящий биосинтез гормонов в щитовидной железе",
        "wiktionary:ru",
    )
    assert q == "Тормозящий биосинтез гормонов в щитовидной железе" and "leading_context_rewritten" in changes
    q, _ = _quality_normalize_definition(
        "вариоскоп", "(при печатании тканей) прибор для комбинирования рисунков", "wiktionary:ru"
    )
    assert q == "Прибор для комбинирования рисунков при печатании тканей"
    q, _ = _quality_normalize_definition("ПРД", "(радио-)передатчик", "wiktionary:ru")
    assert q == "Радиопередатчик"
    q, _ = _quality_normalize_definition("ахейлия", "(врождённое) отсутствие губ", "wiktionary:ru")
    assert q == "Врождённое отсутствие губ"
    q, changes = _quality_normalize_definition(
        "ASCI White",
        "Суперкомпьютер, созданный компанией IBM и установленный в лаборатории в 2001 году.",
        "ruwiki-lead",
    )
    assert q == "Суперкомпьютер." and "wikipedia_history_tail_removed" in changes
    q, changes = _quality_normalize_definition(
        "бобина", "Бобина [1] с намотанной на ней нитью", "wiktionary:ru"
    )
    assert q == "Бобина с намотанной на ней нитью" and "sense_reference_removed" in changes
    long_dal = (
        "Прибаутка, побаска, присказка. Всякая баутка в сказке хороша. "
        "Баутка со смыслом: длинный пример " + "пример " * 100
    )
    q, changes = _quality_normalize_definition("баутка", long_dal, "dal")
    assert q == "Прибаутка, побаска, присказка." and "dal_long_compacted" in changes

    relation_db = ROOT / "_quality_relations47.sqlite3"
    relation_db.unlink(missing_ok=True)
    rel = connect_db(relation_db)
    add_sense(rel, "боец", "Участник боя.", "test"); add_link(rel, "боец", "боец")
    add_sense(rel, "агонист", "Специалист по агонистике.", "test")
    add_sense(rel, "агонист", "То же, что боец", "wiktionary:ru"); add_link(rel, "агонист", "агонист")
    add_sense(rel, "рис", "Злак и его зерно.", "test"); add_link(rel, "рис", "рис"); add_link(rel, "рисе", "рис")
    add_sense(rel, "белое зерно", "О рисе", "wiktionary:ru"); add_link(rel, "белое зерно", "белое зерно")
    add_sense(
        rel, "БОМЖ", "То же, что без определённого места жительства, без прописки (регистрации)", "wiktionary:ru"
    ); add_link(rel, "БОМЖ", "БОМЖ")
    add_sense(rel, "Бомбей", "Вариант названия индийского города Мумбаи", "wiktionary:ru")
    add_link(rel, "Бомбей", "Бомбей")
    add_sense(rel, "Мумбаи", "Крупный город Индии.", "test")
    add_link(rel, "Мумбаи", "Мумбаи")
    st47 = semantic_quality_pass(rel)
    assert st47["textual_aliases_converted"] >= 1
    assert st47["about_aliases_converted"] == 1
    assert st47["alias_fallback_definitions"] == 1
    assert rel.execute("SELECT definition FROM senses WHERE lemma='агонист'").fetchone()[0] == "Специалист по агонистике."
    assert rel.execute("SELECT 1 FROM links WHERE key='агонист' AND lemma='боец'").fetchone()
    assert rel.execute("SELECT COUNT(*) FROM senses WHERE lemma='белое зерно'").fetchone()[0] == 0
    assert rel.execute("SELECT 1 FROM links WHERE key='белое зерно' AND lemma='рис'").fetchone()
    assert rel.execute("SELECT definition FROM senses WHERE lemma='БОМЖ'").fetchone()[0].startswith("Без определённого места")
    assert rel.execute("SELECT COUNT(*) FROM senses WHERE lemma='Бомбей'").fetchone()[0] == 0
    assert rel.execute("SELECT 1 FROM links WHERE key='Бомбей' AND lemma='Мумбаи'").fetchone()
    rel.close(); relation_db.unlink(missing_ok=True)

    # DB-aware nominal rewrite: inflected lexical tokens are changed only when
    # every token has one morphology-graph target; stopwords stay untouched and
    # clause-like punctuation aborts the rewrite.
    phrase_db = ROOT / "_quality_about_phrase.sqlite3"
    phrase_db.unlink(missing_ok=True)
    pc = connect_db(phrase_db)
    for target in ("секс", "СССР", "мужской", "половой", "член", "низший", "слой", "население"):
        add_sense(pc, target, "Смысл слова.", "test")
        add_link(pc, target, target)
    for form, target in (
        ("сексе", "секс"), ("мужском", "мужской"),
        ("половом", "половой"), ("члене", "член"),
        ("низших", "низший"), ("слоях", "слой"), ("населения", "население"),
    ):
        add_link(pc, form, target)
    assert _quality_rewrite_about_phrase(pc, "О сексе в СССР") == ("Секс в СССР", True)
    assert _quality_rewrite_about_phrase(pc, "О мужском половом члене") == ("Мужской половой член", True)
    # A noun ending in ``-ой`` must not be mistaken for an adjective when
    # reconstructing agreement (``слой`` is the real-world regression).
    assert _quality_rewrite_about_phrase(pc, "О низших слоях населения")[1] is False
    assert _quality_rewrite_about_phrase(pc, "О деятельном, неутомимом человеке")[1] is False
    add_sense(pc, "тестовая фраза", "О сексе в СССР", "wiktionary:ru")
    add_link(pc, "тестовая фраза", "тестовая фраза")
    semantic_quality_pass(pc)
    assert pc.execute(
        "SELECT definition FROM senses WHERE lemma='тестовая фраза'"
    ).fetchone()[0] == "Секс в СССР"
    pc.close(); phrase_db.unlink(missing_ok=True)

    # Candidate selection must match the case-insensitive quality detector.  A
    # capitalized source lead must not bypass the headword-prefix cleanup just
    # because the stored lemma is lowercase.
    case_db = ROOT / "_quality_casefold_candidate.sqlite3"
    case_db.unlink(missing_ok=True)
    cc = connect_db(case_db)
    add_sense(
        cc,
        "блок-станция",
        "Блок-станция — это электростанция, не находящаяся в хозяйственном подчинении.",
        "wiktionary:ru",
    )
    add_link(cc, "блок-станция", "блок-станция")
    semantic_quality_pass(cc)
    case_definition = cc.execute(
        "SELECT definition FROM senses WHERE lemma='блок-станция'"
    ).fetchone()[0]
    assert case_definition.startswith("Электростанция")
    cc.close(); case_db.unlink(missing_ok=True)

    # 4.6 QA queues: useful concise reference entries must not occupy the main
    # actionable review file. They are retained in separate informational files.
    qa_db = ROOT / "_quality_review_queues.sqlite3"
    qa_out = ROOT / "_quality_review_queues"
    qa_db.unlink(missing_ok=True); shutil.rmtree(qa_out, ignore_errors=True)
    qac = connect_db(qa_db)
    add_sense(qac, "Абаза", "Фамилия", "wiktionary:ru")
    add_sense(qac, "Божий дом", "Церковь", "wiktionary:ru")
    add_sense(qac, "Арид", "Провинция в", "wiktionary:ru")
    qr = write_quality_report(qac, qa_out, review_limit=100, onomastic_limit=100)
    main_review = (qa_out / "QUALITY_REVIEW.tsv").read_text(encoding="utf-8")
    ono_review = (qa_out / "QUALITY_ONOMASTICS.tsv").read_text(encoding="utf-8")
    concise_review = (qa_out / "QUALITY_CONCISE.tsv").read_text(encoding="utf-8")
    assert "Арид" in main_review and "Абаза" not in main_review and "Божий дом" not in main_review
    assert "Абаза" in ono_review and "Божий дом" in concise_review
    assert qr["informational_counts"]["onomastic_stub"] == 1
    assert qr["informational_counts"]["concise_gloss"] == 1
    qac.close(); qa_db.unlink(missing_ok=True); shutil.rmtree(qa_out, ignore_errors=True)

    dal_mig_db = ROOT / "_quality_dal_migration.sqlite3"
    dal_mig_db.unlink(missing_ok=True)
    dc = connect_db(dal_mig_db)
    add_sense(dc, "азбука", "Система письменных знаков.", "wiktionary:ru")
    add_link(dc, "азбука", "азбука")
    add_sense(dc, "азбука ж", "Слишком длинная резервная статья Даля.", "dal")
    add_link(dc, "азбука ж", "азбука ж")
    st = semantic_quality_pass(dc)
    assert st["dal_fallback_conflicts_removed"] == 1
    assert dc.execute("SELECT COUNT(*) FROM senses WHERE lemma='азбука ж'").fetchone()[0] == 0
    assert not dc.execute("SELECT 1 FROM links WHERE key='азбука ж'").fetchone()
    dc.close(); dal_mig_db.unlink(missing_ok=True)

    # 4.5 post-clean rescue: a placeholder that made Wikipedia look "already
    # defined" may be removed by the semantic pass.  The compact prepared cache
    # can then restore an exact-title definition without rereading the 6-GB dump.
    rescue_db = ROOT / "_quality_rescue.sqlite3"
    rescue_wp = ROOT / "_quality_rescue_wiki.sqlite3"
    rescue_db.unlink(missing_ok=True); rescue_wp.unlink(missing_ok=True)
    rc = connect_db(rescue_db)
    add_sense(rc, "термтест", "Сокр.", "wiktionary:ru")
    add_link(rc, "термтест", "термтест")
    add_sense(rc, "гистерезис-тест", "Свойство системы, не сразу реагирующей на внешнее воздействие.", "wiktionary:ru")
    add_link(rc, "гистерезис-тест", "гистерезис-тест")
    semantic_quality_pass(rc)
    wp = sqlite3.connect(rescue_wp)
    wp.execute("CREATE TABLE candidates(title TEXT PRIMARY KEY, categories TEXT NOT NULL, lead_z BLOB NOT NULL) WITHOUT ROWID")
    rows = [
        (
            "термтест", "Информатика",
            "'''Термтест''' — технический термин для проверки точечного восстановления определения.",
        ),
        (
            "гистерезис-тест", "Физика",
            "'''Гистерезис-тест''' — зависимость состояния системы от истории предшествующих воздействий при одинаковом текущем воздействии.",
        ),
    ]
    for title, cats, lead in rows:
        wp.execute(
            "INSERT INTO candidates(title,categories,lead_z) VALUES (?,?,?)",
            (title, cats, sqlite3.Binary(zlib.compress(lead.encode("utf-8"), 1))),
        )
    wp.commit(); wp.close()
    rst = wikipedia_quality_rescue(rescue_wp, rc)
    assert rst["missing_definitions_rescued"] == 1
    assert rst["weak_definitions_upgraded"] == 1
    assert "технический термин" in rc.execute("SELECT definition FROM senses WHERE lemma='термтест'").fetchone()[0].casefold()
    assert "истории предшествующих воздействий" in rc.execute("SELECT definition FROM senses WHERE lemma='гистерезис-тест'").fetchone()[0].casefold()
    rc.close(); rescue_db.unlink(missing_ok=True); rescue_wp.unlink(missing_ok=True)

    assert lookup(base, "сервитут")[2].startswith("Ограниченное право")
    assert not lookup(base, "сервитут")[2].startswith("(")

    # Conservative quality upgrade: a vague one-sense Wiktionary definition can
    # be replaced by a clearly stronger specialist Wikipedia lead.
    hyst = lookup(base, "гистерезис")[2]
    assert "истории предшествующих воздействий" in hyst.casefold()
    assert "не сразу реагирующ" not in hyst.casefold()

    # Wikipedia redirects become aliases only when the target survives as a real
    # dictionary article. This adds acronyms/synonyms without encyclopedic noise.
    assert lookup(base, "криогеника")[:2] == lookup(base, "криогенная техника")[:2]

    assert (OUT / "QUALITY_REPORT.json").exists()
    assert (OUT / "QUALITY_REPORT.txt").exists()
    assert (OUT / "QUALITY_REVIEW.tsv").exists()
    assert (OUT / "QUALITY_ONOMASTICS.tsv").exists()
    assert (OUT / "QUALITY_CONCISE.tsv").exists()
    quality_report = json.loads((OUT / "QUALITY_REPORT.json").read_text(encoding="utf-8"))
    assert quality_report["average_quality_score"] < 90
    assert quality_report["score_scale"] == "raw-heuristic-v1"
    assert "raw_average_quality_score" not in quality_report
    assert "score_calibration" not in quality_report
    assert isinstance(quality_report.get("warning_counts"), dict)
    review_lines = (OUT / "QUALITY_REVIEW.tsv").read_text(encoding="utf-8").splitlines()
    assert review_lines and review_lines[0].startswith("score\twarnings\tword\tsource\tdefinition")

    from validate_stardict import leaked_grammar_line, leaked_nonmeaning_line
    leaked = "1. Страд. прич. прош. вр. от осветить\n2. Такой, где есть освещение"
    assert leaked_grammar_line(leaked) == "Страд. прич. прош. вр. от осветить"
    assert leaked_nonmeaning_line("1. Сокр.\n2. Нормальное значение") == "Сокр."
    assert leaked_nonmeaning_line("Город в") == "Город в"
    assert leaked_nonmeaning_line("Нормальное значение.") is None


    # 4.8 regressions from the real 4.7 QUALITY_REVIEW.
    q, changes = _quality_normalize_definition("безлюдье", "Об отсутствии людей где-либо", "wiktionary:ru")
    assert q == "Отсутствие людей где-либо" and "about_fragment_rewritten" in changes
    q, _ = _quality_normalize_definition("покой", "О состоянии душевного покоя", "wiktionary:ru")
    assert q == "Состояние душевного покоя"
    q, changes = _quality_normalize_definition("индивидуалистический", "(?)", "wiktionary:ru")
    assert q == "" and "broken_stub_removed" in changes
    q, changes = _quality_normalize_definition("гребло", "??", "wiktionary:ru")
    assert q == "" and "broken_stub_removed" in changes
    q, changes = _quality_normalize_definition("овощ", "() овощ", "wiktionary:ru")
    assert q == "" and "broken_stub_removed" in changes
    q, changes = _quality_normalize_definition("сноска", "См.", "wiktionary:ru")
    assert q == "" and "broken_stub_removed" in changes
    q, changes = _quality_normalize_definition("повреждённый", "[незакрытый фрагмент", "wiktionary:ru")
    assert q == "" and "broken_stub_removed" in changes
    q, changes = _quality_normalize_definition("над", "Над ()", "wiktionary:ru")
    assert q == "" and "broken_stub_removed" in changes
    # 4.8.3 bad-residue scrub: remove only empty delimiters, isolated bullets,
    # dangling colons, and an explicitly unbalanced quote at the text edge.
    for lemma, raw, expected in (
        ("обращение", "Обращение ()", "Обращение"),
        ("прихорашиваться", "Украшаться ()", "Украшаться"),
        ("в основном", "· как правило", "Как правило"),
        ("издыхать", "· умирать", "Умирать"),
        ("авансировать", "Выдавать аванс:", "Выдавать аванс"),
        ("меч", "Ритор., :", "Ритор."),
        ("молвиться", "Говориться || :", "Говориться"),
        ("рассеяние", "Рассеянность \"", "Рассеянность"),
    ):
        q, changes = _quality_normalize_definition(lemma, raw, "wiktionary:ru")
        assert q == expected and "bad_residue_removed" in changes, (raw, q, changes)
    assert _quality_normalize_definition("x", "()", "wiktionary:ru")[0] == ""
    assert _quality_normalize_definition("x", "[]", "wiktionary:ru")[0] == ""
    assert "bad_residue" in definition_quality_flags("x", "Выдавать аванс:", "wiktionary:ru")
    assert "bad_residue" not in definition_quality_flags("x", "word: value", "wiktionary:ru")
    assert "bad_residue" not in definition_quality_flags("x", "[1]", "wiktionary:ru")
    assert "bad_residue" not in definition_quality_flags("x", "«полный термин»", "wiktionary:ru")
    assert "bad_residue" not in definition_quality_flags("x", "A · B", "wiktionary:ru")
    assert _quality_normalize_definition("x", "word: value", "wiktionary:ru")[0] == "Word: value"
    assert _quality_normalize_definition("x", "«полный термин»", "wiktionary:ru")[0] == "«полный термин»"
    assert _quality_normalize_definition("x", "A · B", "wiktionary:ru")[0] == "A · B"
    q, changes = _quality_normalize_definition(
        "головка",
        "Техн. элемент детали или узла в конструкции многих технических устройств, а также в радиоэлектронных устройствах записи и воспроизведения информации : ; :",
        "wiktionary:ru",
    )
    assert q.endswith("воспроизведения информации") and "bad_residue_removed" in changes
    q, changes = _quality_normalize_definition(
        "одарённый", "Страд. прич. прош. вр. от одарить", "wiktionary:ru"
    )
    assert q == "" and "broken_stub_removed" not in changes
    q, _ = _quality_normalize_definition("бровастый", "О том, у кого густые брови", "wiktionary:ru")
    assert q == "Тот, у кого густые брови"
    q, changes = _quality_normalize_definition(
        "амфипротонный",
        "(о растворителе) молекулы которого способны как отдавать, так и принимать протон",
        "wiktionary:ru",
    )
    assert q.startswith("Растворитель, молекулы которого способны")
    assert "leading_context_rewritten" in changes
    prose_variant = "Вариант формы черепа человека, характеризующийся относительно большим поперечным диаметром черепа"
    assert _parse_alias_formula(prose_variant) is None
    assert "redirect_residue" not in definition_quality_flags("брахикрания", prose_variant, "wiktionary:ru")
    q, _ = _quality_normalize_definition("ампутировать", "(кого-либо) подвергать ампутации", "wiktionary:ru-old")
    assert q == "Подвергать кого-либо ампутации"

    print("ALL TESTS PASSED")
    shutil.rmtree(OUT, ignore_errors=True)
    LEGACY.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
