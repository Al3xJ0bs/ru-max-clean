#!/usr/bin/env python3
"""Build RU Max Clean: a Russian StarDict dictionary for KOReader.

Displayed articles contain meanings only. Inflected forms and spelling variants are
lookup keys only. The builder streams Wiktextract/Kaikki and other enabled sources
into SQLite, then writes a compact StarDict index where aliases reuse canonical
article byte ranges instead of duplicating definition text.
"""
from __future__ import annotations

import argparse
import bz2
import datetime as dt
import gzip
import hashlib
import heapq
import io
import shutil
import time
import html
import json
import os
import re
import sqlite3
import struct
import tarfile
import sys
import ctypes
import unicodedata
from concurrent.futures import ProcessPoolExecutor
from collections import deque
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

# Optional native accelerators. bootstrap.py installs these automatically on Windows,
# but the builder retains stdlib fallbacks so an offline machine is never blocked.
try:
    import orjson as _orjson  # type: ignore
except Exception:
    _orjson = None
try:
    from lxml import etree as _lxml_etree  # type: ignore
except Exception:
    _lxml_etree = None
try:
    import indexed_bzip2 as _indexed_bzip2  # type: ignore
except Exception:
    _indexed_bzip2 = None
try:
    import rapidgzip as _rapidgzip  # type: ignore
except Exception:
    _rapidgzip = None

from source_manager import SourceCache
from stage_cache import StageCache, ArtifactCache, file_fingerprint, files_fingerprint, signature as stage_signature
from progress_ui import ProgressTotals, render as progress_render, finish as progress_finish
import human_report as report

from version_info import BUILDER_VERSION

# These versions are deliberately independent from BUILDER_VERSION. A future
# presentation/reporting-only release can therefore reuse expensive parsed stages.
LEXICAL_STAGE_RULES = "lexical-v2-sources"
WIKIPEDIA_STAGE_RULES = "wikipedia-v1"
RESOLVE_STAGE_RULES = "resolve-v1"
QUALITY_STAGE_RULES = "semantic-clean-v10"
FORM_STAGE_RULES = "form-v1"
EXPORT_STAGE_RULES = "stardict-export-v2"
QUALITY_AUDIT_RULES = "quality-audit-v11"


def _builder_code_sha256() -> str:
    """Invalidate clean/form/artifact caches when semantic code changes."""
    digest = hashlib.sha256()
    with Path(__file__).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

KAIKKI_URL = "https://kaikki.org/ruwiktionary/raw-wiktextract-data.jsonl.gz"
WIKIDATA_LEXEMES_URL = "https://dumps.wikimedia.org/wikidatawiki/entities/latest-lexemes.json.bz2"
RUWIKI_URL = "https://dumps.wikimedia.org/ruwiki/latest/ruwiki-latest-pages-articles.xml.bz2"
RUSSIAN_LANGUAGE_QID = "Q7737"

DAL_URLS = (
    "https://stardict.nchrs.xyz/ru/stardict-dal-ru-2.4.2.tar.bz2",
    "https://gitlab.com/avsej/dicts-stardict-form-xdxf/raw/d636cc5e8d4a47e22ac7466f4af6d435a8a3f650/001/stardict-atla02_rus-rus_dalf-2.4.2.tar.gz",
    "https://sourceforge.net/projects/xdxf/files/dicts-stardict-form-xdxf/001/stardict-atla02_rus-rus_dalf-2.4.2.tar.bz2/download",
)
DEFAULT_LANGS = ("ru", "ru-old", "orv", "cu")

# Progress estimates are only UI hints. Actual totals from every completed build are
# persisted in sources/progress_totals.json and replace these defaults.
DEFAULT_PROGRESS_TOTALS = {
    "wiktionary_records": 2_703_147,
    "wikidata_entities": 1_550_647,
    "wikipedia_pages": 6_377_605,
}
_PROGRESS = ProgressTotals(dict(DEFAULT_PROGRESS_TOTALS))
_PROGRESS_PATH: Path | None = None
COMMIT_EVERY = 250_000
# Wiktextract records are intentionally parsed in one streaming pass, but issuing
# one SQLite statement for every alias/form makes a full Russian dump take hours.
# The Kaikki writer below batches rows while keeping the same INSERT OR IGNORE
# semantics.  A bounded flush prevents a large source from growing an unbounded
# Python set.
KAIKKI_BATCH_RECORDS = 25_000
KAIKKI_BATCH_ROWS = 180_000


def json_loads_fast(data):
    if _orjson is not None:
        return _orjson.loads(data)
    return json.loads(data)


def open_gzip_binary_fast(path: Path):
    """Open gzip with parallel decoding when rapidgzip is available."""
    if _rapidgzip is not None and str(path).endswith(".gz"):
        try:
            return _rapidgzip.open(str(path), parallelization=max(1, os.cpu_count() or 1))
        except Exception:
            pass
    return gzip.open(path, "rb")


def open_gzip_text_fast(path: Path):
    return io.TextIOWrapper(open_gzip_binary_fast(path), encoding="utf-8", errors="replace")


def open_bz2_binary_fast(path: Path):
    """Open bzip2 with all-core decoding when indexed_bzip2 is available.

    Python's stdlib bz2 decoder is single-threaded. indexed_bzip2 is optional and
    may not have a wheel for every brand-new Python release, so failure always
    falls back to bz2.open.
    """
    if _indexed_bzip2 is not None and str(path).endswith(".bz2"):
        try:
            # indexed_bzip2 uses the serial backend when parallelization == 1.
            # Pass an explicit worker count >=2 instead of relying on version-
            # specific interpretations of 0. This matters when a fresh
            # Wikidata/Wikipedia dump must be read.
            workers = max(2, min(32, os.cpu_count() or 2))
            return _indexed_bzip2.open(str(path), parallelization=workers)
        except Exception:
            pass
    return bz2.open(path, "rb")


def open_bz2_text_fast(path: Path):
    raw = open_bz2_binary_fast(path)
    return io.TextIOWrapper(raw, encoding="utf-8", errors="replace")


class BuildTimings:
    def __init__(self):
        self.started = time.perf_counter()
        self.items: list[dict[str, object]] = []

    def run(self, name: str, fn, *args, cached: bool = False, **kwargs):
        t0 = time.perf_counter()
        print(f"\n[ЭТАП] {name}: запуск", flush=True)
        result = fn(*args, **kwargs)
        elapsed = time.perf_counter() - t0
        self.items.append({"stage": name, "seconds": round(elapsed, 3), "cached": bool(cached)})
        print(f"[ЭТАП] {name}: завершён за {elapsed:.1f} с", flush=True)
        return result

    def mark(self, name: str, seconds: float, *, cached: bool = False) -> None:
        self.items.append({"stage": name, "seconds": round(seconds, 3), "cached": bool(cached)})

    def total(self) -> float:
        return time.perf_counter() - self.started


def is_json_decode_error(exc: BaseException) -> bool:
    if isinstance(exc, json.JSONDecodeError):
        return True
    return _orjson is not None and isinstance(exc, _orjson.JSONDecodeError)


def xml_iterparse_end(fh, local_tag: str):
    """Fast streaming XML iterator with an lxml fallback when installed."""
    if _lxml_etree is not None:
        # Wildcard namespace syntax supported by lxml. recover/huge_tree are useful
        # for multi-gigabyte Wikimedia dumps.
        context = _lxml_etree.iterparse(
            fh, events=("end",), tag=f"{{*}}{local_tag}", recover=True, huge_tree=True
        )
        for event, elem in context:
            yield event, elem
            elem.clear()
            parent = elem.getparent()
            if parent is not None:
                while elem.getprevious() is not None:
                    del parent[0]
        return
    for event, elem in ET.iterparse(fh, events=("end",)):
        if elem.tag.rsplit("}", 1)[-1] == local_tag:
            yield event, elem


def accelerator_info() -> dict[str, object]:
    info = {
        "orjson": bool(_orjson is not None),
        "lxml": bool(_lxml_etree is not None),
        "indexed_bzip2": bool(_indexed_bzip2 is not None),
        "rapidgzip": bool(_rapidgzip is not None),
        **sqlite_tuning(),
    }
    info["gzip_threads"] = max(1, min(32, os.cpu_count() or 1)) if _rapidgzip is not None else 1
    info["bzip2_threads"] = max(2, min(32, os.cpu_count() or 2)) if _indexed_bzip2 is not None else 1
    return info


def load_progress_totals(cache_dir: Path) -> None:
    global _PROGRESS, _PROGRESS_PATH
    _PROGRESS_PATH = cache_dir / "progress_totals.json"
    values = dict(DEFAULT_PROGRESS_TOTALS)
    try:
        raw = json.loads(_PROGRESS_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, int) and v > 0:
                    values[k] = v
    except Exception:
        pass
    _PROGRESS = ProgressTotals(values)


def save_progress_totals() -> None:
    if _PROGRESS_PATH is None:
        return
    try:
        _PROGRESS_PATH.write_text(json.dumps(_PROGRESS.values, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def progress_expected(key: str, current: int) -> int:
    """Return a useful estimate even when a cache came from a tiny fixture build.

    Release archives can include ``sources/progress_totals.json`` produced by a
    demo/QA run.  If that file says, for example, that Wiktionary has 18 records,
    a real dump immediately renders as 100% and then appears frozen for minutes.
    Grow an undersized estimate as soon as it is exceeded; the final exact count
    is still persisted by ``ProgressTotals.record`` at stage completion.
    """
    expected = _PROGRESS.expected(key)
    if current > expected:
        baseline = DEFAULT_PROGRESS_TOTALS.get(key, 0)
        expected = max(current * 2, baseline, 1)
        _PROGRESS.values[key] = expected
    return expected


def _system_memory_bytes() -> int:
    """Best-effort physical RAM size without third-party packages."""
    if os.name == "nt":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys)
        except Exception:
            pass
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except Exception:
        return 0


def sqlite_tuning() -> dict[str, int]:
    cpus = max(1, os.cpu_count() or 1)
    ram = _system_memory_bytes()
    ram_mib = ram // (1024 * 1024) if ram else 0
    workers = max(2, min(32, cpus))
    if ram_mib:
        cache_mib = max(512, min(4096, ram_mib // 8))
        mmap_mib = max(1024, min(8192, ram_mib // 5))
    else:
        cache_mib, mmap_mib = 768, 2048
    return {"cpus": cpus, "workers": workers, "ram_mib": ram_mib, "cache_mib": cache_mib, "mmap_mib": mmap_mib}

# Cyrillic + Latin (including accented letters) + Greek. Scientific/technical
# tokens such as pH, USB, H2O and Greek-letter terms are deliberately allowed;
# purely numeric keys are rejected. Extended Latin matters for French/German
# inserts in translated prose (e.g. ``è``, ``à``, ``Brüder``).
LETTER_RE = re.compile(r"[A-Za-z\u00c0-\u024f\u1e00-\u1eff\u0370-\u03ff\u0400-\u052f]")
WS_RE = re.compile(r"\s+")
WIKI_LINK_RE = re.compile(r"\[\[([^\]|]+)\|([^\]]+)\]\]|\[\[([^\]]+)\]\]")
TEMPLATE_RE = re.compile(r"\{\{[^{}]*\}\}")
HTML_TAG_NAMES = (
    r"a|abbr|b|blockquote|br|code|dd|del|div|dl|dt|em|font|h[1-6]|hr|i|ins|"
    r"kbd|li|mark|ol|p|pre|q|s|small|span|strike|strong|sub|sup|table|tbody|"
    r"td|tfoot|th|thead|tr|tt|u|ul|var"
)
HTML_PAIR_RE = re.compile(
    rf"<({HTML_TAG_NAMES})(?:\s+[^<>]*)?>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
HTML_CLOSE_RE = re.compile(rf"</(?:{HTML_TAG_NAMES})\s*>", re.IGNORECASE)
HTML_VOID_RE = re.compile(r"<(?:br|hr)\b[^<>]*?/?>", re.IGNORECASE)
HTML_ATTR_RE = re.compile(rf"<(?:{HTML_TAG_NAMES})\s+[^<>]*>", re.IGNORECASE)
HTML_KNOWN_TAG_RE = re.compile(
    rf"</?(?:{HTML_TAG_NAMES})(?:\s+[^<>]*)?/?>", re.IGNORECASE
)
LEADING_MARK_RE = re.compile(r"^(?:[#*:;\-]+|\d+[.)])\s*")
# Fallback for rare unstructured form-of glosses. Structured form_of/alt_of
# metadata is the primary filter.  This regex is intentionally conservative:
# grammar terminology itself ("сравнительная степень", "дательный падеж") is
# legitimate lexical content and must remain searchable.
GRAMMAR_TEXT_RE = re.compile(
    r"^(?:"
    # Be deliberately narrow. Structured form_of/alt_of metadata is the primary
    # filter; this fallback catches only unmistakable textual redirects.
    # A normal definition may start with "Форма" and later contain "от".
    r"(?:форма|словоформа)\s+(?:глагола|существительного|прилагательного|причастия|"
    r"деепричастия|местоимения|числительного|наречия)\s+"
    r"(?:от\s+(?:слова|лексемы)\s+)?"
    r"[«\"']?[A-Za-zА-Яа-яЁёІіѢѣѲѳѴѵ-]+[»\"']?[.!]?"
    r"|(?:форма|словоформа)\b.{0,120}\b(?:слова|лексемы)\s+"
    r"[«\"']?[A-Za-zА-Яа-яЁёІіѢѣѲѳѴѵ-]+[»\"']?[.!]?"
    r"|(?:(?:именительный|родительный|дательный|винительный|творительный|предложный)\s+падеж"
    r"|(?:единственное|множественное)\s+число)\b.{0,100}\b(?:слова|лексемы)\s+"
    r"[«\"']?[A-Za-zА-Яа-яЁёІіѢѣѲѳѴѵ-]+[»\"']?[.!]?"
    r")$",
    re.IGNORECASE,
)

# Some Russian Wiktionary entries encode form-of information only as a short
# Russian gloss instead of structured ``form_of`` metadata.  These exact
# patterns appeared in real KOReader testing (e.g. "Страд. прич. прош. вр. от
# вкопать").  We extract the referenced lemma as an alias and suppress the
# grammatical sentence from the popup.  The patterns are deliberately narrow so
# lexical definitions beginning with words such as "Форма" remain untouched.
FORM_TARGET_WORD = r"[A-Za-zА-Яа-яЁёІіѢѣѲѳѴѵ]+(?:-[A-Za-zА-Яа-яЁёІіѢѣѲѳѴѵ]+)*"
TEXTUAL_FORM_OF_RES = (
    re.compile(
        rf"^\s*(?:(?:страд(?:ат(?:ельн(?:ое|ый|ая)?)?)?|действ(?:ительн(?:ое|ый|ая)?)?)\.?\s+)?"
        rf"(?:прич(?:астие)?|деепр(?:ичастие)?)\.?"
        rf"(?:\s+(?:прош(?:едш(?:его|ее|ий)?)?|наст(?:оящ(?:его|ее|ий)?)?|буд(?:ущ(?:его|ее|ий)?)?)\.?)?"
        rf"(?:\s+(?:вр(?:емени)?|времени)\.?)?"
        rf"(?:\s+(?:кратк(?:ая|ое|ие)?|полн(?:ая|ое|ые)?)\.?)?"
        rf"\s+от\s+(?:(?:гл(?:агола)?|слова|лексемы)\.?\s+)?"
        rf"[«\"']?(?P<target>{FORM_TARGET_WORD})[»\"']?\s*[.!]?\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^\s*(?:форма|словоформа)\s+"
        rf"(?:глагола|существительного|прилагательного|причастия|деепричастия|"
        rf"местоимения|числительного|наречия)\s+"
        rf"(?:от\s+(?:слова|лексемы)\s+)?[«\"']?(?P<target>{FORM_TARGET_WORD})[»\"']?\s*[.!]?\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^\s*(?:(?:именительный|родительный|дательный|винительный|творительный|предложный)\s+падеж"
        rf"|(?:единственное|множественное)\s+число)\b.{0,90}?\b(?:слова|лексемы)\s+"
        rf"[«\"']?(?P<target>{FORM_TARGET_WORD})[»\"']?\s*[.!]?\s*$",
        re.IGNORECASE,
    ),
)


def textual_form_target(value: object) -> str | None:
    """Return lemma referenced by an unstructured Russian form-of gloss."""
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFC", html.unescape(value)).translate(DASH_TRANSLATION)
    text = WIKI_LINK_RE.sub(lambda m: m.group(2) or m.group(3) or m.group(1) or "", text)
    text = WS_RE.sub(" ", text).strip()
    for rx in TEXTUAL_FORM_OF_RES:
        m = rx.match(text)
        if m:
            target = normalize_key(m.group("target"))
            return target if is_lookup_key(target) else None
    return None


# Labels that carry register/domain/grammar metadata rather than the meaning itself.
# We strip them only at the very beginning of a gloss and only in abbreviated
# label form, so ordinary words such as "исторический" or "физический" are safe.
LABEL_TOKEN = (
    r"(?:авиац|автомоб|агрон|анат|антропол|археол|архит|астр|биол|бот|бухг|вет|"
    r"воен|геогр|геод|геол|геральд|горн|диал|ж\.-д|зоол|информ|иск|ист|истор|"
    r"картогр|книжн|комп|косм|лингв|лит|мат|мед|металл|метеорол|микробиол|"
    r"минер|мифол|мор|муз|неодобр|обл|опт|перен|полигр|полит|прост|псих|радио|"
    r"разг|редк|рел|религ|с\.-х|социол|спорт|спец|строит|театр|тех|устар|фарм|"
    r"физ|физиол|филос|фин|фотогр|хим|экол|экон|электр|этногр|юр|шутл|ирон|"
    r"бран|вульг|поэт|высок|возвыш|офиц|канц|публиц|проф|жарг|сленг|детск|"
    r"охотн|рыб|кулин|текст|типогр|телеком|прогр|инж|мех|букв|ласк|пренебр|"
    r"уничиж|эвф|груб|фольк|народн|адъектив|субстантив|предикатив)\."
)
LEADING_LABEL_RE = re.compile(
    rf"^\s*(?:(?:[\[(](?:{LABEL_TOKEN})(?:\s*[,;/]\s*(?:{LABEL_TOKEN}))*[\])])|"
    rf"(?:(?:{LABEL_TOKEN})(?:\s*[,;/]\s*(?:{LABEL_TOKEN}))*))"
    rf"(?:\s*[:;,—-]?\s*)",
    re.IGNORECASE,
)
# Old dictionaries often begin with POS and source-language abbreviations.
LEGACY_META_TOKEN = (
    r"(?:м|ж|ср|об|мн|ед|гл|прил|нареч|мест|предл|союз|част|межд|прич|деепр|"
    r"франц|нем|лат|греч|англ|итал|исп|польск|татар|тур|араб|перс|голл|"
    r"церк|слав|стар|древн|монг|фин|швед|норв)\."
)
LEADING_LEGACY_META_RE = re.compile(
    rf"^\s*(?:(?:{LEGACY_META_TOKEN})\s*[,;:]?\s*)+", re.IGNORECASE
)
DASH_TRANSLATION = str.maketrans({
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2212": "-",
    "\u00a0": " ", "\u202f": " ",
})


def contains_probable_html(text: str) -> bool:
    """Detect actual markup without treating linguistic/scientific <x> notation as HTML."""
    return bool(
        HTML_PAIR_RE.search(text)
        or HTML_CLOSE_RE.search(text)
        or HTML_VOID_RE.search(text)
        or HTML_ATTR_RE.search(text)
    )


class TextOnlyHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join(self.parts)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def normalize_key(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    value = value.translate(DASH_TRANSLATION)
    return WS_RE.sub(" ", value.strip())


def is_lookup_key(value: object) -> bool:
    if not isinstance(value, str):
        return False
    value = normalize_key(value)
    if not value or len(value) > 220 or "\x00" in value or "\n" in value or "\r" in value:
        return False
    # Entries written with an edge hyphen are affixes/morphemes (e.g. "-у",
    # "без-") rather than standalone words.  Internal hyphens remain valid,
    # so technical terms such as "PID-регулятор" are unaffected.
    if value.startswith("-") or value.endswith("-"):
        return False
    return bool(LETTER_RE.search(value))


def strip_combining_alias(value: str) -> str | None:
    decomposed = unicodedata.normalize("NFD", value)
    # Remove lexical stress marks, but preserve phonemic combining marks such
    # as Cyrillic breve in ``й`` (NFD: ``и`` + U+0306).  Removing every Mn
    # character silently turned ``руко́й`` into the incorrect alias ``рукои``.
    stress_marks = {"\u0300", "\u0301", "\u0340", "\u0341"}
    stripped = "".join(ch for ch in decomposed if ch not in stress_marks)
    stripped = unicodedata.normalize("NFC", stripped)
    return stripped if stripped != value else None


def yo_alias(value: str) -> str | None:
    alt = value.replace("ё", "е").replace("Ё", "Е")
    return alt if alt != value else None


def strip_leading_labels(text: str) -> str:
    """Remove leading usage/domain labels such as ``истор.`` or ``физ.``.

    The user-facing dictionary is intentionally definition-only.  Register and
    domain tags are useful during lexicographic processing, but they should not
    occupy the small KOReader popup.
    """
    previous = None
    while text != previous:
        previous = text
        text = LEADING_LABEL_RE.sub("", text, count=1)
    return text.lstrip(" ,;:—-")


DOMAIN_CONTEXT_ROOTS = re.compile(
    r"(?:\u0437\u0435\u043c\u0435\u043b\u044c\u043d|\u043f\u0440\u0430\u0432|\u044e\u0440\u0438\u0434|\u0444\u0438\u0437\u0438\u043a|\u043c\u0430\u0442\u0435\u043c\u0430\u0442|\u0445\u0438\u043c|\u043c\u0435\u0434\u0438\u0446|\u0431\u0438\u043e\u043b\u043e\u0433|\u0433\u0435\u043e\u043b|\u0433\u0435\u043e\u0434\u0435\u0437|"
    r"\u044d\u043b\u0435\u043a\u0442\u0440|\u044d\u043b\u0435\u043a\u0442\u0440\u043e\u043d|\u043c\u0435\u0445\u0430\u043d|\u0442\u0435\u0445\u043d\u0438\u043a|\u0438\u043d\u0436\u0435\u043d\u0435\u0440|\u0441\u0442\u0440\u043e\u0438\u0442|\u043c\u0435\u0442\u0430\u043b\u043b\u0443\u0440\u0433|\u044d\u043d\u0435\u0440\u0433|"
    r"\u0438\u043d\u0444\u043e\u0440\u043c\u0430\u0442|\u043f\u0440\u043e\u0433\u0440\u0430\u043c|\u043a\u043e\u043c\u043f\u044c\u044e\u0442|\u0442\u0435\u043b\u0435\u043a\u043e\u043c|\u044d\u043a\u043e\u043d\u043e\u043c|\u0444\u0438\u043d\u0430\u043d\u0441|\u0431\u0430\u043d\u043a|\u0431\u0443\u0445\u0433\u0430\u043b\u0442|"
    r"\u0430\u0432\u0438\u0430|\u043c\u043e\u0440\u0441\u043a|\u0436\u0435\u043b\u0435\u0437\u043d\u043e\u0434\u043e\u0440\u043e\u0436|\u0430\u0432\u0442\u043e\u043c\u043e\u0431|\u0432\u043e\u0435\u043d|\u043a\u0440\u0438\u043c\u0438\u043d\u0430\u043b|\u043b\u0438\u043d\u0433\u0432|\u044f\u0437\u044b\u043a\u043e\u0437\u043d|\u0444\u0438\u043b\u043e\u0441\u043e\u0444|"
    r"\u043f\u0441\u0438\u0445\u043e\u043b|\u0441\u043e\u0446\u0438\u043e\u043b|\u0441\u0442\u0430\u0442\u0438\u0441\u0442|\u043c\u0435\u0442\u0440\u043e\u043b|\u0441\u0442\u0430\u043d\u0434\u0430\u0440\u0442|\u043e\u043f\u0442\u0438\u043a|\u0430\u043a\u0443\u0441\u0442|\u0433\u0438\u0434\u0440\u0430\u0432\u043b|\u043f\u043d\u0435\u0432\u043c\u0430\u0442|"
    r"\u0442\u0435\u043f\u043b\u043e\u0442\u0435\u0445|\u043a\u0440\u0438\u043e\u0433\u0435\u043d|\u043b\u0430\u0437\u0435\u0440|\u043f\u043e\u043b\u0443\u043f\u0440\u043e\u0432\u043e\u0434|\u044f\u0434\u0435\u0440\u043d|\u043a\u0432\u0430\u043d\u0442|\u0444\u0430\u0440\u043c|\u0430\u043d\u0430\u0442\u043e\u043c|\u0444\u0438\u0437\u0438\u043e\u043b|\u0434\u0438\u0430\u0433\u043d\u043e\u0441\u0442|\u0442\u0435\u0440\u0430\u043f|"
    r"\u0445\u0438\u0440\u0443\u0440\u0433|\u044d\u043a\u043e\u043b\u043e\u0433|\u0430\u0441\u0442\u0440\u043e\u043d\u043e\u043c|\u043a\u043e\u0441\u043c|\u043c\u0430\u0442\u0435\u0440\u0438\u0430\u043b\u043e\u0432\u0435\u0434|\u043d\u0435\u0444\u0442|\u0433\u0430\u0437\u043e\u0432|\u0433\u043e\u0440\u043d|\u0441\u0435\u043b\u044c\u0441\u043a|\u0430\u0433\u0440\u043e\u043d)"
    r"",
    re.IGNORECASE,
)
LEADING_PAREN_CONTEXT_RE = re.compile(r"^\s*[\[(]([^\])]{2,100})[\])]\s*")
YEAR_RE = re.compile(r"\b(?:1[5-9]\d{2}|20\d{2})\b")
VAGUE_DEFINITION_RE = re.compile(
    r"^(?:\u0441\u0432\u043e\u0439\u0441\u0442\u0432\u043e|\u0441\u043f\u043e\u0441\u043e\u0431\u043d\u043e\u0441\u0442\u044c|\u044f\u0432\u043b\u0435\u043d\u0438\u0435|\u043f\u0440\u043e\u0446\u0435\u0441\u0441).{0,130}\b(?:\u043d\u0435\s+\u0441\u0440\u0430\u0437\u0443|\u043a\u0430\u043a\u0438\u043c-\u043b\u0438\u0431\u043e\s+\u043e\u0431\u0440\u0430\u0437\u043e\u043c|\u043a\u0430\u043a\u0438\u043c-\u043b\u0438\u0431\u043e\s+\u0441\u043f\u043e\u0441\u043e\u0431\u043e\u043c)\b",
    re.IGNORECASE,
)
PARTICIPLE_ENDINGS = (
    "\u0451\u043d\u043d\u044b\u0439", "\u0435\u043d\u043d\u044b\u0439", "\u0430\u043d\u043d\u044b\u0439", "\u044f\u043d\u043d\u044b\u0439", "\u043d\u0443\u0442\u044b\u0439", "\u0442\u044b\u0439", "\u043d\u043d\u044b\u0439",
)
LEADING_INFINITIVE_CHAIN_RE = re.compile(
    r"^(?P<verbs>[\u0410-\u042f\u0401\u0430-\u044f\u0451-]+(?:\u0442\u044c|\u0442\u0438|\u0447\u044c)(?:\s+(?:\u0438|\u0438\u043b\u0438)\s+[\u0410-\u042f\u0401\u0430-\u044f\u0451-]+(?:\u0442\u044c|\u0442\u0438|\u0447\u044c)){0,2})(?P<rest>\s+.+)$",
    re.IGNORECASE,
)

# 4.5 semantic cleanup rules are intentionally post-source.  This allows an older
# lexical/max parse cache to be upgraded without rereading multi-gigabyte dumps.
EXAMPLE_TAIL_RE = re.compile(r"\s*[◆◇]\s*.*$", re.DOTALL)
NKRJA_TAIL_RE = re.compile(r"\s*\[(?:НКРЯ|Google Books|источник[^\]]*)\].*$", re.IGNORECASE | re.DOTALL)
INLINE_CITATION_RE = re.compile(r"\s*\[(?:\d{1,3}|уточнить|источник[^\]]*)\]")
# Narrow post-cleanup residue detectors.  These are deliberately limited to
# empty delimiters, isolated list bullets, dangling colons, and an unbalanced
# quote at the edge of a definition.  They must not touch normal punctuation,
# citation references, mathematical notation, or balanced quotations.
BAD_RESIDUE_EMPTY_GROUP_RE = re.compile(r"(?<!\w)(?:\(\s*\)|\[\s*\])(?!\w)")
BAD_RESIDUE_LEADING_BULLET_RE = re.compile(r"^\s*[·•]\s*")
BAD_RESIDUE_TRAILING_BULLET_RE = re.compile(r"\s+[·•]\s*$")
BAD_RESIDUE_INLINE_COLON_RE = re.compile(r"\s+(?:[,;|]\s*)+:\s*(?=$|[A-Za-zА-Яа-яЁёІіѢѣѲѳѴѵ])")
BAD_RESIDUE_TRAILING_COLON_RE = re.compile(r":\s*$")
BAD_RESIDUE_PUNCT_TAIL_RE = re.compile(r"\s+(?:[:;,|]\s*){2,}$")
SHORT_ALIAS_RE = re.compile(r"^(?:к\s+)?([A-Za-zА-Яа-яЁёІіѢѣѲѳѴѵ][A-Za-zА-Яа-яЁёІіѢѣѲѳѴѵ'’.-]{1,80})[.!]?$", re.IGNORECASE)
EXPLICIT_ALIAS_RE = re.compile(
    r"^(?:то\s+же,?\s+что(?:\s+и)?|см\.?|"
    r"вариант\s+(?:написания|названия|именования|формы|имени)|устар\.?\s+вариант|"
    r"гипокор\.?\s+к|уменьш\.?\s+к|димин\.?\s+к|к)\s+"
    r"(.{1,160}?)[.!]?$",
    re.IGNORECASE,
)
ALIAS_PREFIX_RE = re.compile(
    r"^(?P<kind>то\s+же,?\s+что(?:\s+и)?|см\.?|"
    r"вариант\s+(?:написания|названия|именования|формы|имени)|"
    r"устар\.?\s+вариант|гипокор\.?\s+к|уменьш\.?\s+к|димин\.?\s+к|к)\s+"
    r"(?P<body>.+?)\s*[.!]?$",
    re.IGNORECASE,
)
BARE_VARIANT_ALIAS_RE = re.compile(
    # Historical/civil-script Wiktionary uses many Cyrillic code points outside
    # the small modern-Russian ranges (ѹ, ꙑ, ѧ, etc.).  Restrict by *shape* here
    # (one token, no punctuation that can introduce prose) and let is_lookup_key()
    # perform the Unicode-aware letter check below.  This keeps
    # "Вариант бѹкварь" as an alias while rejecting prose such as
    # "Вариант фонемы в слабой позиции".
    r"^вариант\s+(?P<body>[^\s,;:()]{2,100})[.!]?$",
    re.IGNORECASE,
)
ALIAS_POS_PREFIX_RE = re.compile(
    r"^(?:(?:прил|сущ|нареч|гл|глаг|местоим|числ|имя|существительному|прилагательному|аббревиатуре)\.?\s+)+", re.IGNORECASE
)
ALIAS_TAIL_CLASS_RE = re.compile(
    r"^(?:город|село|деревня|пос[ёе]лок|река|озеро|гора|остров|полуостров|регион|"
    r"область|провинция|кантон|штат|страна|государство|столица|курорт|вулкан|"
    r"мужское\s+имя|женское\s+имя|фамилия|отчество|термин|название|"
    r"набор|система|устройство|программа|праздник|персонаж|явление)\b",
    re.IGNORECASE,
)
ABOUT_FRAGMENT_RE = re.compile(r"^(?:о|об|обо)\s+(.+?)[.!]?$", re.IGNORECASE)
BROKEN_MEANINGLESS_STUB_RE = re.compile(
    r"^(?:\(\?\)|\?{1,3}|см\.?|\[?хорошо|\(\)\s*[^а-яёa-z0-9]{0,4}|то\.?|сленг\s*\?)$",
    re.IGNORECASE,
)
ABOUT_SAFE_CLASS_PREFIX_RE = re.compile(
    r"^(?:городе|селе|деревне|реке|озере|горе|острове|полуострове|рыбе|птице|"
    r"растении|животном|насекомом)\s+(.+)$", re.IGNORECASE
)
# 4.8: high-confidence nominal heads for Wiktionary glosses written as
# "О/Об <prepositional noun> ...".  These are semantic descriptions, not
# dictionary metadata.  Rewriting only the first nominal head preserves the
# complement verbatim and avoids guessing adjective agreement.
ABOUT_NOMINAL_HEADS = {
    "человеке": "Человек", "женщине": "Женщина", "мужчине": "Мужчина",
    "девушке": "Девушка", "ребёнке": "Ребёнок", "ребенке": "Ребёнок",
    "подростке": "Подросток", "лице": "Лицо", "месте": "Место",
    "состоянии": "Состояние", "ощущении": "Ощущение", "наличии": "Наличие",
    "отсутствии": "Отсутствие", "желании": "Желание", "необходимости": "Необходимость",
    "возможности": "Возможность", "чувстве": "Чувство", "звуке": "Звук",
    "голосе": "Голос", "крике": "Крик", "пении": "Пение", "начале": "Начало",
    "сборе": "Сбор", "корне": "Корень", "цвете": "Цвет", "времени": "Время",
    "ситуации": "Ситуация", "ошибке": "Ошибка", "случае": "Случай",
    "способе": "Способ", "явлении": "Явление", "процессе": "Процесс",
    "действии": "Действие", "движении": "Движение", "свойстве": "Свойство",
    "форме": "Форма", "смысле": "Смысл", "значении": "Значение",
}
ABOUT_NOMINAL_RE = re.compile(
    r"^(?:о|об|обо)\s+(?P<head>[А-Яа-яЁё-]+)(?P<rest>(?:\s|[,;:—–-]).*)?$", re.IGNORECASE
)
# Common parenthetical selectional restrictions that can be integrated without
# losing syntax: "(о растворителе) молекулы которого ..." ->
# "Растворитель, молекулы которого ...".
PAREN_DEPENDENT_HEADS = {
    "о растворителе": "Растворитель", "об объекте": "Объект",
    "о веществе": "Вещество", "о системе": "Система", "об устройстве": "Устройство",
    "о молекуле": "Молекула", "о молекулах": "Молекулы",
}
INLINE_SENSE_REF_RE = re.compile(r"\s*\[(?:[1-9]|[1-9]\d)\](?=\s|[,.;:—–-]|$)")
DAL_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[А-ЯЁA-Z0-9«(])")
DAL_OBVIOUS_EXAMPLE_RE = re.compile(
    r"^(?:я|ты|он|она|мы|вы|они|кто|что|как|где|коли|ежели|если|не|дай|дайте|"
    r"погляди|посмотри|хороша|хорошо|жив[ёе]м|стоит|эка|уж|ну)\b", re.IGNORECASE
)
DAL_DERIVATIVE_META_RE = re.compile(
    r"\b(?:умалит|ласкат|бранн|прилаг|нареч|сущ|глаг|действ\.|страдат\.|"
    r"относящ|собират|мн\.|ед\.|ср\.|м\.|ж\.)\b", re.IGNORECASE
)
DAL_HEADWORD_POS_SUFFIX_RE = re.compile(r"\s+(?:м|ж|ср|об|мн|ед)\.?\s*$", re.IGNORECASE)
DAL_LEADING_REGION_RE = re.compile(
    r"^(?:арх|астрах|влад|вологодск|вор|вят|калуж|камч|костр|ниж|новг|олон|"
    r"пенз|перм|ряз|сиб|смол|твер|тул|южн)\.\s*[,;:]?\s*",
    re.IGNORECASE,
)
DAL_LEADING_OLD_USAGE_RE = re.compile(
    r"^стар\.\s*[,]?\s*(?:а\s+местами\s*\([^)]{1,60}\)\s+и\s+ныне\s*:?\s*)?",
    re.IGNORECASE,
)
EMPTY_META_DEFINITION_RE = re.compile(r"^(?:сокр(?:ащение)?\.?|аббр\.?|имя\s+собственное\.?)$", re.IGNORECASE)
FRAGMENT_DEFINITION_RE = re.compile(r"^(?:гора|город|река|деревня|село|пос[ёе]лок|округ|область|провинция|регион|штат|остров|озеро|ручей|приток)\s+(?:в|во|на|из)$", re.IGNORECASE)
ABOUT_PREAMBLE_RE = re.compile(r"^о\s+(?:расстрел|истори|происхожд|употреблен|написан|вариант|значени|случа|назван)\w*\b", re.IGNORECASE)
LEADING_META_PAREN_RE = re.compile(
    r"^\s*\((?P<meta>[^()]{1,180})\)\s*[,;:—–-]*\s*(?P<rest>.+)$", re.DOTALL
)
META_PAREN_CONTENT_RE = re.compile(
    r"(?:^(?:м\.?\s*р\.?|ж\.?\s*р\.?|ср\.?\s*р\.?|индекс\b)|"
    r"^(?:субстантив|адъектив|предикатив|жарг|сокращ|аббревиат)\w*\.?\b|"
    r"^(?:от\s+)?(?:древне|поздне|средне)?(?:англ|лат|греч|древнегреч|нем|фр|франц|исп|итал|кит|китай|яп|польск|чешск|"
    r"укр|белор|араб|перс|тур|турецк|татарск|казах|ингуш|чечен|мадьярск|венгерск|венгр|чудск|санскр|иврит|порт|нидерл|швед|норв|дат)\.?\b|"
    r"перевод\w*\s+с\s+(?:англ|лат|греч|нем|фр|исп|итал)\.?\b|"
    r'пиньин|транслит|букв\.?\s*[«"]|в\s+(?:древне)?(?:греч|египет|шумер|'
    r"скандинав|иран|славян|удмурт).*мифолог|в\s+[а-яё-]{3,32}\s+мифолог|в\s+мифолог|"
    r"в\s+библи|в\s+индуизм|в\s+зороастр|в\s+кришна|в\s+эпос|в\s+древности\b|"
    r"^у\s+[а-яё-]{3,32}\b|^(?:рег|регион|диал|обл|разг|прост|устар|редк)\.?\??$|"
    r"^(?:кому|кого|чего|что|чем|кем|где|куда|откуда|о\s+ком|о\s+ч[её]м)$|"
    r"(?:\b(?:лат|англ|греч|нем|фр|исп|итал)\.)[^)]*(?:перевод|от\b))",
    re.IGNORECASE,
)
BAD_REMAINDER_START_RE = re.compile(
    r"^(?:выпущенн|созданн|разработанн|основанн|построенн|представленн|известн|"
    r"провед[ёе]нн|состоявш|запланирован|приуроченн|проявляющ)\w*\b", re.IGNORECASE
)
DAL_NEXT_HEADWORD_RE = re.compile(
    r"(?<=[.!?])\s+(?=[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z'’.-]{2,50}"
    r"(?:\s*,\s*[А-ЯЁа-яёA-Za-z'’.-]{2,50}){0,4}\s+(?:м|ж|ср)\.)"
)
WIKI_POST_ENTITY_NOISE_RE = re.compile(
    r"^(?:.*?\b)?(?:раунд\s+сезона|этап\s+(?:сезона|серии|чемпионата)|"
    r"(?:автомобильная|автомобильный|ежегодная|ежегодный)?\s*гонка\b|"
    r"чемпионат\s+мира\b|киберспортивная\s+команда\b|"
    r"(?:музыкальный\s+)?сборник\b|альбом\b|музыкальная\s+группа\b|"
    r"джазовое\s+трио\b|торговый\s+центр\b|пятизв[ёе]здочный\s+отель\b|"
    r"праздничн(?:ое|ые)\s+мероприят|фестиваль\b)",
    re.IGNORECASE,
)
WIKI_EVENT_TITLE_RE = re.compile(
    r"(?:\b(?:19|20)\d{2}\b|^\d+\s+(?:час(?:а|ов)?|км)\b|"
    r"(?:чемпионат|world championship|серия\s+ле-ман|ле-мана)\b)",
    re.IGNORECASE,
)
# High-confidence historical tails that do not help identify the concept in a
# quick dictionary popup. Only trim such a tail when it also contains a year.
WIKI_HISTORY_TAIL_START_RE = re.compile(
    r",\s+(?:разработан|создан|выпущен|принят|построен|основан|представлен|"
    r"спроектирован|анонсирован|введ[ёе]н|изобрет[ёе]н|открыт|провед[ёе]н|"
    r"состоявш|выпускавш|производивш)\w*\b",
    re.IGNORECASE,
)
WIKI_NAMED_SHIP_RE = re.compile(r"^(?:HMS|USS|SMS|HMAS|RMS)\b", re.IGNORECASE)


# Quality 4.6: distinguish harmless concise onomastic meanings from genuinely
# broken/placeholder definitions.  These entries are useful when reading fiction
# (a tap on an unfamiliar surname or settlement still answers what it is), but
# they should not drown the actionable QA queue or depress the global score.
ONOMASTIC_STUB_RE = re.compile(
    r"^(?:"
    r"(?:мужское|женское|личное|русское|греческое|английское|еврейское|арабское|"
    r"индийское|японское|узбекское|болгарское|британское|испанское)\s+имя|"
    r"(?:русская|английская|испанская|караимская|немецкая|французская|итальянская)?\s*фамилия|"
    r"имя|топоним|название\s+(?:реки|горы|озера|города|ветра)|"
    r"(?:река|село|деревня|город|озеро|гора|пос[ёе]лок|округ|область|штат|провинция|тауншип)\s+(?:в|во|на)\s+.+|"
    r"(?:столица|приток|ручей|остров|мыс|залив|полуостров)\s+.+|"
    r"(?:древнее|античное|средневековое)\s+(?:царство|государство|город)|"
    r"(?:римский|греческий|скифский|болгарский|еврейский|арабский)\s+(?:номен|антропоним|имя)|"
    r"[а-яё-]{3,32}(?:ское|ская)\s+(?:имя|фамилия)|"
    r"кличка\s+.+"
    r")[.!]?$",
    re.IGNORECASE,
)
OLD_EQUIV_PLACEHOLDER_RE = re.compile(
    r"^(.{1,120}?)\s*\((?:аналогично\s+(?:русскому\s+слову|современным\s+значениям?)|"
    r"аналогично\s+рус\.?(?:скому)?\s+слову)(?:[^)]*)\)\s*$",
    re.IGNORECASE,
)
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
ABBREV_EXPANSION_RE = re.compile(
    r"^(?:сокр(?:ащение)?\.?|аббр\.?)\s*(?:от\s+)?(.+)$", re.IGNORECASE
)
LEADING_ABOUT_RE = re.compile(r"^\(\s*о\s+([^()]{1,80})\)\s*(.+)$", re.IGNORECASE | re.DOTALL)
LEADING_HISTORICAL_RANGE_RE = re.compile(
    r"^\((?P<context>(?:до|с)\s+(?:1[5-9]\d{2}|20\d{2})(?:-х)?\s*(?:г\.?|гг\.?|года|годов)?)\)\s*(?P<rest>.+)$",
    re.IGNORECASE | re.DOTALL,
)
# A year should look like prose, not a protocol/model token such as IMT-2020.
HUMAN_YEAR_RE = re.compile(
    r"(?<![A-Za-zА-Яа-яЁё0-9/_-])(?:1[5-9]\d{2}|20\d{2})(?![A-Za-zА-Яа-яЁё0-9/_-])"
)
WIKI_DANGLING_TAIL_RE = re.compile(
    r"(?:\(\s*см\.?|\bсм\.|\bсокр\.|\bг\.|\bиз|\bкомпанией|\bкорпорацией|\bфирмой)\s*$",
    re.IGNORECASE,
)
WIKI_STRAY_MEDIA_TAIL_RE = re.compile(
    r"\s*[.)]*\s*(?:на\s+почтовой\s+марке|на\s+фотографии|на\s+иллюстрации)\s*$",
    re.IGNORECASE,
)
WIKI_LIST_RESIDUE_RE = re.compile(r"(?:\s+\*\s+.*){2,}", re.DOTALL)
TRAILING_HEADWORD_DESCRIPTOR_RE = re.compile(
    r"^(?P<head>[^()]{1,120}?)\s*\((?P<desc>[^()]{2,100})\)\s*[.!]?$", re.DOTALL
)
TRAILING_DESCRIPTOR_NOUN_RE = re.compile(
    r"\b(?:фамили|имя|раздел|глава|город|село|деревн|река|озеро|гора|остров|район|"
    r"область|провинц|штат|регион|термин|название|прозвище|кличка|титул|звание)\w*\b",
    re.IGNORECASE,
)
CONCISE_GLOSS_RE = re.compile(
    r"^[A-Za-zА-Яа-яЁёІіѢѣѲѳѴѵ0-9«»'’.-]+(?:[ ,—–/-]+[A-Za-zА-Яа-яЁёІіѢѣѲѳѴѵ0-9«»'’.-]+){0,5}[.!]?$"
)
# Broader history clauses than 4.5.  Keep the conceptual class and remove dated
# manufacture/adoption history when the class before the clause is informative.
WIKI_HISTORY_CLAUSE_RE = re.compile(
    r",\s+(?:(?:котор(?:ый|ая|ое|ые)\s+)?(?:был[аио]?\s+)?)?"
    r"(?:разработан|создан|выпущен|принят|построен|основан|представлен|спроектирован|"
    r"анонсирован|введ[ёе]н|изобрет[ёе]н|открыт|запущен|установлен|поступил|"
    r"выпускавш|выпускающ|производивш|производим|издававш)\w*\b",
    re.IGNORECASE,
)
WIKI_HISTORY_INLINE_RE = re.compile(
    r"\s+(?:созданн|разработанн|выпущенн|производим|выпускаем|выпускавш|построенн|"
    r"принят|запущенн|установленн)\w*\b",
    re.IGNORECASE,
)
GENERIC_WIKI_CORES = {
    "автомобиль", "компьютер", "программа", "проект", "сервис", "сайт", "веб-сайт",
    "компания", "организация", "спутник", "устройство", "система", "приложение",
}


def strip_leading_context_parenthetical(text: str) -> str:
    """Remove a leading parenthesized domain qualifier, but not semantic clauses."""
    previous = None
    while text != previous:
        previous = text
        m = LEADING_PAREN_CONTEXT_RE.match(text)
        if not m:
            break
        content = WS_RE.sub(" ", m.group(1)).strip().casefold()
        if not DOMAIN_CONTEXT_ROOTS.search(content):
            break
        if not re.match(r"^(?:\u0432|\u0432\u043e|\u0434\u043b\u044f|\u043f\u0440\u0438|\u0432\s+\u043e\u0431\u043b\u0430\u0441\u0442\u0438|\u0432\s+\u0441\u0444\u0435\u0440\u0435)\b", content):
            break
        text = text[m.end():].lstrip(" ,;:\u2014-")
    return text


def _compact_quality_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    # Regex substitution on every one of ~600k definitions was a major 4.5 QA
    # bottleneck.  Most database strings are already compact, so only pay for it
    # when whitespace actually needs normalization.
    if "  " in text or "\n" in text or "\r" in text or "\t" in text:
        text = WS_RE.sub(" ", text)
    return text.strip()


def _has_unbalanced_edge_quote(text: str) -> bool:
    """Return True only for an obviously unfinished quote at a text edge.

    A dangling straight quote after whitespace is a frequent extraction tail
    (for example ``Рассеянность \"``).  Requiring an odd count and an edge
    position avoids changing inch marks such as ``5\"`` or balanced prose.
    Curly quote pairs are checked asymmetrically for the same reason.
    """
    text = _compact_quality_text(text)
    if not text:
        return False
    straight_count = text.count('"')
    if straight_count % 2:
        if text.endswith('"') and (len(text) == 1 or text[-2].isspace() or text[-2] in ".,;:!?)]}"):
            return True
        if text.startswith('"') and (len(text) == 1 or text[1].isalpha() or text[1].isspace()):
            return True
    for opening, closing in (("«", "»"), ("„", "“"), ("“", "”"), ("‘", "’")):
        opened = text.count(opening)
        closed = text.count(closing)
        if opened > closed and text.startswith(opening):
            return True
        if closed > opened and text.endswith(closing):
            return True
    return False


def _has_bad_residue(text: str) -> bool:
    """Detect narrowly-scoped punctuation/placeholder residue after cleaning."""
    text = _compact_quality_text(text)
    if not text:
        return False
    return bool(
        BAD_RESIDUE_EMPTY_GROUP_RE.search(text)
        or BAD_RESIDUE_LEADING_BULLET_RE.match(text)
        or BAD_RESIDUE_TRAILING_BULLET_RE.search(text)
        or BAD_RESIDUE_INLINE_COLON_RE.search(text)
        or BAD_RESIDUE_TRAILING_COLON_RE.search(text)
        or BAD_RESIDUE_PUNCT_TAIL_RE.search(text)
        or _has_unbalanced_edge_quote(text)
    )


def _strip_bad_residue(text: str) -> tuple[str, bool]:
    """Remove only empty/dangling extraction residue, preserving real prose."""
    text = _compact_quality_text(text)
    if not text:
        return "", False
    original = text
    for _ in range(2):
        text = BAD_RESIDUE_EMPTY_GROUP_RE.sub(" ", text)
        text = BAD_RESIDUE_LEADING_BULLET_RE.sub("", text)
        text = BAD_RESIDUE_TRAILING_BULLET_RE.sub("", text)
        text = BAD_RESIDUE_INLINE_COLON_RE.sub(" ", text)
        text = BAD_RESIDUE_TRAILING_COLON_RE.sub("", text)
        text = BAD_RESIDUE_PUNCT_TAIL_RE.sub("", text)
        text = WS_RE.sub(" ", text).strip(" \t\r\n;,|•")
        if not text:
            break

    # Strip a quote only when the helper has proven an edge imbalance.  Keep
    # matched quotes and ordinary apostrophes unchanged.
    if text and _has_unbalanced_edge_quote(text):
        if text.endswith('"'):
            text = text[:-1].rstrip()
        elif text.startswith('"'):
            text = text[1:].lstrip()
        elif text.startswith(("«", "„", "‚", "“", "‘")):
            text = text[1:].lstrip()
        elif text.endswith(("»", "“", "’", "”")):
            text = text[:-1].rstrip()
        text = WS_RE.sub(" ", text).strip(" \t\r\n;,|•")

    return text, text != original


def _split_leading_parenthetical(text: str) -> tuple[str, str] | None:
    """Split one balanced leading ``(...)``/``[...]`` wrapper, including nesting.

    Several real QA rows contain nested spelling/etymology notes such as
    ``(Ольги́н (Holguín)) испанская фамилия``.  A flat regex stops at the inner
    parenthesis and leaves the metadata visible.  This tiny scanner is deliberately
    local to the first wrapper and never rewrites text on its own.
    """
    text = str(text or "").lstrip()
    if not text or text[0] not in "([":
        return None
    opening = text[0]
    closing = ")" if opening == "(" else "]"
    depth = 0
    for i, ch in enumerate(text):
        if ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                meta = _compact_quality_text(text[1:i])
                rest = _compact_quality_text(text[i + 1:]).lstrip(" ,;:—–-")
                return (meta, rest) if meta and rest else None
    return None



def _parse_alias_formula(value: object) -> tuple[str, list[str], str, str] | None:
    """Parse a textual redirect without mistaking lexical prose for a redirect.

    Returns ``(kind, candidates, semantic_tail, original_body)``.  The parser is
    deliberately syntactic; the database-aware step later decides which candidate
    actually resolves to a real article.  In particular, plain prose beginning
    with "Вариант кодирования ..." is *not* an alias, while the historical
    Wiktionary form "Вариант бдеющій" is.
    """
    text = _compact_quality_text(value).strip(" \t\r\n")
    if not text:
        return None
    m = ALIAS_PREFIX_RE.fullmatch(text)
    bare_variant = False
    if not m:
        vm = BARE_VARIANT_ALIAS_RE.fullmatch(text)
        if not vm:
            return None
        kind = "вариант"
        body = _compact_quality_text(vm.group("body"))
        bare_variant = True
    else:
        kind = _compact_quality_text(m.group("kind")).casefold()
        body = _compact_quality_text(m.group("body"))
    body = body.strip(" .;:—–-")
    if not body:
        return None
    # "Вариант формы черепа человека ..." is ordinary lexical prose, while
    # historical redirects such as "Вариант формы бдеющій" are short targets.
    # Do not classify long semantic noun phrases as redirects.
    if kind.startswith("вариант формы") and len(body.split()) > 4:
        return None

    # POS words are source metadata, not part of the target lemma:
    # "К прил. бледный" -> "бледный".
    body = ALIAS_POS_PREFIX_RE.sub("", body).strip()
    if not body:
        return None

    # Modern Wiktionary exports sometimes wrap a proper-name target in an
    # explanatory phrase, e.g. "Вариант названия индийского города Мумбаи"
    # or "Вариант написания города Каликут, расположенного ...".  Keep this
    # conservative: only the named-target forms after the explicit variant
    # marker are extracted; the DB-aware resolver still requires one unique
    # article before converting the row into an alias.
    variant_named_target = None
    if kind.startswith(("вариант названия", "вариант именования", "вариант написания", "вариант имени")):
        vm = re.match(
            r"^.*?"
            r"(?P<target>[А-ЯЁ][А-Яа-яЁёІіѢѣѲѳѴѵ'’.-]{1,80}"
            r"(?:\s+[А-ЯЁ][А-Яа-яЁёІіѢѣѲѳѴѵ'’.-]{1,80})?)"
            r"(?:\s*,.*)?$",
            body,
        )
        if vm:
            variant_named_target = vm.group("target").strip()

    tail = ""
    target_part = body
    if ";" in body:
        left, right = body.split(";", 1)
        if left.strip() and right.strip():
            target_part, tail = left.strip(), right.strip()
    elif "," in body:
        left, right = body.split(",", 1)
        if ALIAS_TAIL_CLASS_RE.match(right.strip()):
            target_part, tail = left.strip(), right.strip()

    # "То же, что BIOS набор микропрограмм ..." occasionally misses punctuation.
    # Accept the first token as a target only when the remainder visibly starts a
    # dictionary class; otherwise keep the whole body as the fallback meaning.
    if not tail and kind.startswith("то же"):
        first, sep, rest = target_part.partition(" ")
        if sep and ALIAS_TAIL_CLASS_RE.match(rest.strip()) and is_lookup_key(first):
            target_part, tail = first.strip(), rest.strip()

    if variant_named_target:
        target_part = variant_named_target
        tail = ""

    candidates: list[str] = []
    if bare_variant:
        candidates = [target_part]
    elif kind.startswith(("к", "гипокор", "уменьш", "димин", "см")):
        split_re = r"\s*,\s*|\s+или\s+"
        if kind.startswith("см") and re.fullmatch(r"[A-Za-zА-Яа-яЁёІіѢѣѲѳѴѵ'’.-]+(?:\s+и\s+[A-Za-zА-Яа-яЁёІіѢѣѲѳѴѵ'’.-]+){1,3}", target_part, re.IGNORECASE):
            split_re += r"|\s+и\s+"
        candidates = [x.strip(" .;:—–-") for x in re.split(split_re, target_part, flags=re.IGNORECASE) if x.strip()]
    elif kind.startswith("то же") and "," in target_part:
        # It may be a list of synonymous targets. If none resolve, the original
        # body remains available as a semantic fallback.
        candidates = [x.strip(" .;:—–-") for x in target_part.split(",") if x.strip()]
    else:
        candidates = [target_part]
    candidates = [normalize_key(x) for x in candidates if is_lookup_key(x)]
    if not candidates and not tail:
        return None
    return kind, candidates, tail, body


def _rewrite_about_fragment(text: str) -> tuple[str, bool]:
    """Rewrite structurally safe ``О том, кто ...`` glosses into direct meaning.

    Referential one-noun glosses (``О коте``) are handled database-aware as
    aliases later, because the inflected noun can be resolved through the existing
    morphology graph without guessing Russian inflection rules.
    """
    compact = _compact_quality_text(text)
    replacements = (
        (r"^о\s+том,?\s+кто\s+", "Тот, кто "),
        (r"^о\s+том,?\s+что\s+", "То, что "),
        (r"^о\s+том,?\s+у\s+кого\s+", "Тот, у кого "),
        (r"^о\s+том,?\s+у\s+чего\s+", "То, у чего "),
        (r"^о\s+ч[её]м-либо,?\s+что\s+", "Что-либо, что "),
        (r"^о\s+ком-либо,?\s+кто\s+", "Кто-либо, кто "),
    )
    for pattern, repl in replacements:
        newer = re.sub(pattern, repl, compact, count=1, flags=re.IGNORECASE)
        if newer != compact:
            return newer[0].upper() + newer[1:], True

    # Direct nominal paraphrase for high-confidence heads.  This turns metadata-
    # looking glosses such as "Об отсутствии людей" into the actual meaning
    # "Отсутствие людей" without needing a general Russian inflector.
    nm = ABOUT_NOMINAL_RE.fullmatch(compact)
    if nm:
        repl = ABOUT_NOMINAL_HEADS.get(nm.group("head").casefold())
        raw_rest = nm.group("rest") or ""
        if repl:
            # Comma/colon/dash continuations often contain adjectives or
            # participles still agreeing with the prepositional head
            # ("о женщине, находящейся..."). Rewriting only the noun would make
            # them ungrammatical, so leave those for QA instead of guessing.
            stripped = raw_rest.lstrip()
            if stripped.startswith((",", ":", "—", "–", "-")) or stripped.casefold().startswith("или "):
                return compact, False
            rest = _compact_quality_text(raw_rest)
            body = repl + ((" " + rest) if rest else "")
            body = re.sub(r"\s+([,.;:])", r"\1", body).strip()
            return body, True
    return compact, False


def _historical_years(text: str) -> set[str]:
    """Years that look like prose dates rather than referenced model numbers."""
    out: set[str] = set()
    for m in HUMAN_YEAR_RE.finditer(text):
        prefix = text[max(0, m.start() - 36):m.start()].casefold()
        if re.search(r"(?:модел[ьи]|модель|индекс(?:а)?|тип(?:а)?|серии)\s+[a-zа-яё0-9./+-]*$", prefix):
            continue
        out.add(m.group(0))
    return out


def _dal_compact_long_definition(lemma: str, text: str) -> str:
    """Keep the dictionary core of a very long Dal article.

    Dal often places the definition first and then examples, proverbs, derivative
    words and encyclopedic commentary in the same StarDict article.  The routine
    never cuts in the middle of a clause: it keeps at most three complete initial
    sentences and, for an oversized first sentence, only complete semicolon
    clauses.  It is intentionally limited to already-long (>520 char) fallback
    articles so ordinary rare-word coverage is untouched.
    """
    text = _compact_quality_text(text)
    if len(text) <= 520:
        return text
    sentences = [x.strip() for x in DAL_SENTENCE_SPLIT_RE.split(text) if x.strip()]
    if not sentences:
        return text

    first = sentences[0]
    if len(first) > 420 and ";" in first:
        parts = [x.strip() for x in first.split(";") if x.strip()]
        kept_parts: list[str] = []
        for part in parts:
            candidate = "; ".join(kept_parts + [part])
            if kept_parts and len(candidate) > 420:
                break
            kept_parts.append(part)
            if len(candidate) >= 320:
                break
        if kept_parts:
            first = "; ".join(kept_parts).rstrip(" .;:") + "."

    kept = [first]
    total = len(first)
    for sentence in sentences[1:3]:
        if total >= 420:
            break
        probe = sentence.strip(' «"')
        if DAL_OBVIOUS_EXAMPLE_RE.match(probe):
            break
        if any(mark in sentence for mark in ('"', '«', '»')) and len(sentence) > 80:
            break
        # After the primary Dal sentence, retain only clearly marked regional /
        # professional secondary senses. Ordinary following prose is usually an
        # example, proverb, derivative or explanation rather than another meaning.
        secondary_marked = bool(re.match(
            r"^(?:арх|астрах|влад|вологодск|вор|вят|калуж|камч|костр|ниж|новг|олон|"
            r"пенз|перм|пск|ряз|сиб|смол|твер|тул|южн|зап|вост|моск|морск|торг|церк|стар)\.",
            probe, re.IGNORECASE,
        ))
        if not secondary_marked:
            break
        if DAL_DERIVATIVE_META_RE.search(sentence) and total >= 120:
            break
        if total + 1 + len(sentence) > 420:
            break
        kept.append(sentence)
        total += 1 + len(sentence)
    result = " ".join(kept).strip()
    if len(result) >= 20 and len(text) - len(result) >= 80:
        return result
    return text


def _is_onomastic_stub(text: str) -> bool:
    if not text or len(text) > 140:
        return False
    probe = unicodedata.normalize("NFD", text)
    # Remove stress marks only.  Removing every combining mark would turn й into
    # и and break ordinary words such as "индийское".
    probe = "".join(ch for ch in probe if ch not in {"\u0300", "\u0301", "\u0341"})
    probe = unicodedata.normalize("NFC", probe)
    if ONOMASTIC_STUB_RE.fullmatch(probe):
        return True
    if re.fullmatch(r"(?:фамилия|имя|топоним)\s*\([^()]{1,60}\)[.!]?", probe, re.IGNORECASE):
        return True
    # A few compact proper-name classifications use a genitive country/region
    # without the preposition "в": "Озеро Литвы", "Регион Эфиопии".
    return bool(re.fullmatch(
        r"(?:река|озеро|гора|город|село|деревня|регион|область|провинция|столица)\s+"
        r"[А-ЯЁ][А-ЯЁа-яё'’.-]{2,60}", probe, re.IGNORECASE
    ))


def _is_concise_gloss(text: str) -> bool:
    if not text or not (4 <= len(text) <= 45):
        return False
    low = text.casefold().strip()
    if low.startswith(("о ", "об ", "обо ")):
        return False
    if _parse_alias_formula(text):
        return False
    if EMPTY_META_DEFINITION_RE.fullmatch(text) or FRAGMENT_DEFINITION_RE.fullmatch(text):
        return False
    probe = text
    # A short semantic qualifier in final parentheses is still a perfectly useful
    # compact gloss (e.g. "Греция (вообще)").  Leading parentheses remain QA.
    probe = re.sub(r"\s*\([^()]{1,40}\)\s*[.!]?$", "", probe).rstrip() or probe
    return bool(CONCISE_GLOSS_RE.fullmatch(probe) and sum(ch.isalpha() for ch in probe) >= 3)


def _is_old_equivalence_placeholder(lemma: str, text: str, source: str) -> bool:
    if not source.startswith(("wiktionary:cu", "wiktionary:orv", "wiktionary:ru-old")):
        return False
    m = OLD_EQUIV_PLACEHOLDER_RE.fullmatch(text)
    if not m:
        return False
    lhs = normalize_key(m.group(1)).casefold().strip(" .,:;—–-")
    head = normalize_key(lemma).casefold().strip(" .,:;—–-")
    return bool(lhs and head and (lhs == head or lhs.replace("ѣ", "е").replace("і", "и") == head.replace("ѣ", "е").replace("і", "и")))


def _starts_with_headword(lemma: str, text_low: str) -> bool:
    head = normalize_key(lemma).casefold().strip(" .,:;—–-")
    if not head or len(head) < 4:
        return False
    probe = text_low[4:] if text_low.startswith("это ") else text_low
    if not probe.startswith(head):
        return False
    if len(probe) == len(head):
        return True
    return probe[len(head)].isspace() or probe[len(head)] in "—–:-"


def _human_years(text: str) -> set[str]:
    return {m.group(0) for m in HUMAN_YEAR_RE.finditer(text)}


def definition_quality_flags(lemma: str, definition: str, source: str = "") -> list[str]:
    """Return conservative QA warnings; warnings never delete an article by themselves.

    4.6 keeps this hot path intentionally cheap.  Expensive grammar/HTML regexes are
    guarded by tiny string prefilters, and headword self-reference no longer compiles
    a fresh regular expression for every definition.
    """
    flags: list[str] = []
    text = _compact_quality_text(definition)
    if not text:
        return ["empty"]
    low = text.casefold()
    n = len(text)

    onomastic = _is_onomastic_stub(text)
    concise = _is_concise_gloss(text) if not onomastic else False
    if onomastic:
        flags.append("onomastic_stub")
    if n < 8 and not onomastic and not concise:
        flags.append("very_short")
    if n > 520:
        flags.append("very_long")

    # Grammar-only redirects are rare after the semantic scrub.  Avoid running
    # the full recognizers for ordinary prose.
    if any(token in low for token in ("прич", "падеж", "словоформ", "склон", "спряж", "форма ", " вр. ")):
        if textual_form_target(text) or GRAMMAR_TEXT_RE.fullmatch(text):
            flags.append("grammar_residue")
    if ("<" in text and ">" in text and contains_probable_html(text)) or "{{" in text or "[[" in text:
        flags.append("markup_residue")
    if text[:1] in "([" and _split_leading_parenthetical(text):
        flags.append("leading_parenthetical")
    if low.startswith(("свойство", "способность", "явление", "процесс")) and VAGUE_DEFINITION_RE.search(text):
        flags.append("vague")
    # Short lexicalized expansions such as "Нижний Новгород", "Крёстная мать"
    # or "Блокада Ленинграда" are valid concise meanings, not self-reference.
    if _starts_with_headword(lemma, low) and not concise and not onomastic:
        flags.append("early_self_reference")
    if "◆" in text or "◇" in text or "[НКРЯ]" in text:
        flags.append("example_residue")
    if n <= 80 and EMPTY_META_DEFINITION_RE.fullmatch(text):
        flags.append("placeholder_definition")
    if n <= 100 and FRAGMENT_DEFINITION_RE.fullmatch(text):
        flags.append("fragment")
    if _is_old_equivalence_placeholder(lemma, text, source):
        flags.append("old_equivalence_placeholder")
    if URL_RE.search(text):
        flags.append("url_residue")
    if _parse_alias_formula(text):
        flags.append("redirect_residue")
    if n <= 100 and low.startswith(("о ", "об ", "обо ")):
        flags.append("about_fragment")
    if _has_bad_residue(text):
        flags.append("bad_residue")
    if not flags and concise:
        flags.append("concise_gloss")
    if source.startswith("ruwiki"):
        years = _historical_years(text)
        # A model/title year already present in the headword (e.g. "пушка образца
        # 1998 года") is identification, not automatically encyclopedia noise.
        if years and not years.issubset(_historical_years(lemma)):
            history_signal = bool(
                WIKI_HISTORY_CLAUSE_RE.search(text)
                or WIKI_HISTORY_INLINE_RE.search(text)
                or (n > 220 and re.search(
                    r"\b(?:выпуск|производ|разработ|создан|основан|открыт|запущ|поступил|"
                    r"принят|построен|представлен|действовал|издавал)\w*\b",
                    low, re.IGNORECASE,
                ))
            )
            if history_signal:
                flags.append("encyclopedic_date")
        if WIKI_DANGLING_TAIL_RE.search(text) or abs(text.count("(") - text.count(")")) >= 2:
            flags.append("broken_fragment")
    return flags


def definition_quality_score(
    lemma: str,
    definition: str,
    source: str = "",
    _flags: list[str] | None = None,
    _normalized_text: str | None = None,
) -> int:
    """A coarse 0..100 score used for QA and conservative Wikipedia upgrades."""
    text = _normalized_text if _normalized_text is not None else _compact_quality_text(definition)
    if not text:
        return 0
    flags = set(_flags if _flags is not None else definition_quality_flags(lemma, text, source))
    n = len(text)

    # Concise proper-name classifications are shallow but valid dictionary
    # answers. Treat them as medium-quality rather than as broken fragments.
    if "onomastic_stub" in flags:
        score = 64 if n >= 9 else 56
    elif "concise_gloss" in flags:
        score = 64
    else:
        score = 72
        if n < 16:
            score -= 30
        elif n < 35:
            score -= 12
        elif 45 <= n <= 260:
            score += 8
        elif n > 520:
            score -= 20
        elif n > 360:
            score -= 8

    score -= 45 * ("grammar_residue" in flags)
    score -= 35 * ("markup_residue" in flags)
    score -= 14 * ("vague" in flags)
    score -= 10 * ("early_self_reference" in flags)
    score -= 8 * ("leading_parenthetical" in flags)
    score -= 8 * ("encyclopedic_date" in flags)
    score -= 35 * ("example_residue" in flags)
    score -= 20 * ("placeholder_definition" in flags)
    score -= 30 * ("fragment" in flags)
    score -= 32 * ("old_equivalence_placeholder" in flags)
    score -= 25 * ("url_residue" in flags)
    score -= 25 * ("broken_fragment" in flags)
    score -= 20 * ("redirect_residue" in flags)
    score -= 24 * ("about_fragment" in flags)
    score -= 18 * ("bad_residue" in flags)
    if text.endswith((".", "!", "?")):
        score += 3
    if n >= 55 and any(ch in text for ch in (";", ",", "—")):
        score += 2
    return max(0, min(100, score))


def clean_definition(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = unicodedata.normalize("NFC", html.unescape(text))

    def repl(m: re.Match[str]) -> str:
        return m.group(2) if m.group(2) is not None else (m.group(3) or "")

    text = WIKI_LINK_RE.sub(repl, text)
    for _ in range(6):
        newer = TEMPLATE_RE.sub("", text)
        if newer == text:
            break
        text = newer
    # Malformed/unclosed template tails occur in a tiny number of source entries.
    # They are never part of a definition, so discard the tail rather than exposing
    # raw MediaWiki syntax in KOReader.
    if "{{" in text:
        text = text.split("{{", 1)[0].rstrip()
    text = text.replace("__NOTOC__", " ").replace("]]", " ").replace("[[", " ")
    # Strip real HTML markup, but preserve angle-bracket notation used in
    # linguistics and science, e.g. "<и> — <э>" or "x < y".
    if contains_probable_html(text):
        parser = TextOnlyHTMLParser()
        try:
            parser.feed(text)
            parser.close()
            text = parser.text()
        except Exception:
            text = HTML_KNOWN_TAG_RE.sub(" ", text)
    text = text.translate(DASH_TRANSLATION)
    text = LEADING_MARK_RE.sub("", text.strip())
    text = WS_RE.sub(" ", text).strip(" \t\r\n;,•")
    # Do this before removing leading labels: otherwise
    # "Страд. прич. прош. вр. от вкопать" could degrade to "От вкопать".
    if textual_form_target(text):
        return ""
    text = strip_leading_labels(text)
    text = strip_leading_context_parenthetical(text)
    if text:
        # Labels often leave a lowercase first letter; normal Russian prose in a
        # compact dictionary reads better with a conventional initial capital.
        first = text[0]
        if first.isalpha() and first == first.lower():
            text = first.upper() + text[1:]
    if not text:
        return ""
    low = text.casefold()
    if low in {"?", "-", "--", "нет значения", "значение не указано", "значение неизвестно"}:
        return ""
    # Structured form_of/alt_of records are filtered earlier. This catches a few
    # unstructured grammar-only glosses while retaining real lexical definitions.
    if len(text) < 220 and (textual_form_target(text) or GRAMMAR_TEXT_RE.search(text)):
        return ""
    return text


def open_jsonl(path: Path):
    if str(path).endswith(".gz"):
        return open_gzip_text_fast(path)
    return path.open("rt", encoding="utf-8", errors="replace")


def open_jsonl_binary(path: Path):
    # Avoid UTF-8 decoding millions of source records that can be rejected by a
    # cheap ASCII metadata prefilter before JSON parsing. orjson/json both accept
    # bytes for the records that survive.
    if str(path).endswith(".gz"):
        return open_gzip_binary_fast(path)
    return path.open("rb")


def download(url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(destination.suffix + ".part")
    if temp.exists():
        temp.unlink()
    req = urllib.request.Request(url, headers={"User-Agent": f"RU-Max-Clean/{BUILDER_VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=90) as response, temp.open("wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total:
                    sys.stderr.write(f"\r{destination.name}: {done * 100.0 / total:5.1f}%")
                else:
                    sys.stderr.write(f"\r{destination.name}: {done / 1048576.0:8.1f} MiB")
                sys.stderr.flush()
        temp.replace(destination)
        sys.stderr.write("\n")
        return destination
    except Exception:
        if temp.exists():
            temp.unlink()
        raise


def download_first(urls: Iterable[str], destination: Path) -> Path:
    """Try mirrors in order and verify that a real archive was obtained."""
    errors: list[str] = []
    for url in urls:
        try:
            log(f"Downloading {destination.name} from {url}")
            download(url, destination)
            # Fail fast on HTML landing pages returned by some SourceForge mirrors.
            with destination.open("rb") as f:
                head = f.read(256).lstrip().lower()
            if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
                raise OSError("mirror returned an HTML page instead of the archive")
            return destination
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            if destination.exists():
                destination.unlink()
            log(f"WARNING: mirror failed: {exc}")
    raise OSError("all download mirrors failed: " + " | ".join(errors))


def parse_ifo_text(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.replace("\r", "").split("\n"):
        if "=" in line:
            k, v = line.split("=", 1)
            fields[k.strip()] = v.strip()
    return fields


def _archive_member_bytes(tf: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    fh = tf.extractfile(member)
    if fh is None:
        raise OSError(f"cannot read archive member {member.name}")
    return fh.read()


def load_stardict_bundle(path: Path) -> tuple[dict[str, str], bytes, bytes, bytes | None]:
    """Load a StarDict bundle from .tar.* archive, .ifo file, or directory.

    Returns (ifo_fields, idx_bytes, uncompressed_dict_bytes, syn_bytes).
    Only the compact single-field m/h dictionaries needed by our legacy layers
    are accepted; unsupported multimedia dictionaries are rejected explicitly.
    """
    path = Path(path)
    if path.is_file() and tarfile.is_tarfile(path):
        with tarfile.open(path, "r:*") as tf:
            members = [m for m in tf.getmembers() if m.isfile()]
            ifos = [m for m in members if m.name.lower().endswith(".ifo")]
            if not ifos:
                raise ValueError(f"no .ifo in {path}")
            ifo_m = ifos[0]
            stem = ifo_m.name[:-4]
            by_name = {m.name: m for m in members}
            idx_m = by_name.get(stem + ".idx") or by_name.get(stem + ".idx.gz")
            dict_m = by_name.get(stem + ".dict") or by_name.get(stem + ".dict.dz")
            syn_m = by_name.get(stem + ".syn")
            if idx_m is None or dict_m is None:
                raise ValueError(f"incomplete StarDict bundle in {path}")
            ifo_raw = _archive_member_bytes(tf, ifo_m)
            idx_raw = _archive_member_bytes(tf, idx_m)
            dict_raw = _archive_member_bytes(tf, dict_m)
            syn_raw = _archive_member_bytes(tf, syn_m) if syn_m is not None else None
            if idx_m.name.endswith(".gz"):
                idx_raw = gzip.decompress(idx_raw)
            if dict_m.name.endswith(".dz"):
                dict_raw = gzip.decompress(dict_raw)
    else:
        if path.is_dir():
            ifos = sorted(path.rglob("*.ifo"))
            if not ifos:
                raise ValueError(f"no .ifo under {path}")
            ifo_path = ifos[0]
        elif path.suffix.lower() == ".ifo":
            ifo_path = path
        else:
            raise ValueError(f"unsupported StarDict input: {path}")
        base = ifo_path.with_suffix("")
        ifo_raw = ifo_path.read_bytes()
        idx_path = base.with_suffix(".idx")
        idx_gz = Path(str(base) + ".idx.gz")
        if idx_path.exists():
            idx_raw = idx_path.read_bytes()
        elif idx_gz.exists():
            idx_raw = gzip.decompress(idx_gz.read_bytes())
        else:
            raise ValueError(f"missing .idx for {ifo_path}")
        dict_path = base.with_suffix(".dict")
        dict_dz = Path(str(base) + ".dict.dz")
        if dict_path.exists():
            dict_raw = dict_path.read_bytes()
        elif dict_dz.exists():
            dict_raw = gzip.decompress(dict_dz.read_bytes())
        else:
            raise ValueError(f"missing .dict/.dict.dz for {ifo_path}")
        syn_path = base.with_suffix(".syn")
        syn_raw = syn_path.read_bytes() if syn_path.exists() else None

    ifo_text = ifo_raw.decode("utf-8", errors="replace")
    fields = parse_ifo_text(ifo_text)
    sequence = fields.get("sametypesequence", "m")
    if sequence not in {"m", "h"}:
        raise ValueError(f"unsupported sametypesequence={sequence!r}; only m/h are supported")
    return fields, idx_raw, dict_raw, syn_raw


def parse_stardict_idx(idx_raw: bytes, *, offset_bits: int = 32) -> list[tuple[str, int, int]]:
    entries: list[tuple[str, int, int]] = []
    pos = 0
    trailer = 12 if offset_bits == 64 else 8
    fmt = ">QI" if offset_bits == 64 else ">II"
    while pos < len(idx_raw):
        nul = idx_raw.find(b"\x00", pos)
        if nul < 0 or nul + 1 + trailer > len(idx_raw):
            raise ValueError("corrupt StarDict .idx")
        word = idx_raw[pos:nul].decode("utf-8", errors="replace")
        off, size = struct.unpack_from(fmt, idx_raw, nul + 1)
        entries.append((word, off, size))
        pos = nul + 1 + trailer
    return entries


def parse_stardict_syn(syn_raw: bytes | None) -> list[tuple[str, int]]:
    if not syn_raw:
        return []
    out: list[tuple[str, int]] = []
    pos = 0
    while pos < len(syn_raw):
        nul = syn_raw.find(b"\x00", pos)
        if nul < 0 or nul + 5 > len(syn_raw):
            break
        word = syn_raw[pos:nul].decode("utf-8", errors="replace")
        idx_no = struct.unpack_from(">I", syn_raw, nul + 1)[0]
        out.append((word, idx_no))
        pos = nul + 5
    return out


def decode_stardict_article(raw: bytes, sequence: str) -> str:
    text = raw.rstrip(b"\x00").decode("utf-8", errors="replace")
    if sequence == "h" or contains_probable_html(text):
        # Legacy dictionaries may use tags not present in our conservative main
        # HTML detector, so this path deliberately strips any short tag-looking
        # construct.  It is used only for imported legacy sources.
        text = re.sub(r"<[^<>]{1,100}>", " ", text)
    return html.unescape(text)


def normalize_legacy_headword(word: str, profile: str = "generic") -> str:
    lemma = normalize_key(word)
    if profile == "dal":
        # The converted Dal StarDict sometimes embeds a gender/POS marker in the
        # index key itself ("азбука ж").  It is metadata, not part of the word.
        lemma = DAL_HEADWORD_POS_SUFFIX_RE.sub("", lemma).strip()
    return lemma


def clean_legacy_definition(word: str, article: str, profile: str = "generic") -> str:
    text = unicodedata.normalize("NFC", article).replace("\x00", " ")
    text = text.replace("\r", "\n")
    text = re.sub(r"[\t ]+", " ", text)
    text = re.sub(r"\s*\n+\s*", " ", text).strip()
    # Drop repeated headword at article start, a common StarDict conversion artefact.
    escaped = re.escape(normalize_key(word))
    text = re.sub(rf"^\s*{escaped}\s*[,.;:—-]+\s*", "", text, flags=re.IGNORECASE)
    text = strip_leading_labels(text)
    previous = None
    while text != previous:
        previous = text
        text = LEADING_LEGACY_META_RE.sub("", text, count=1)
        text = strip_leading_labels(text)

    if profile == "dal":
        # Dal entries often continue from the definition into examples/proverbs,
        # usually after a double bar. Keep the explanatory part only.
        text = re.split(r"\s*\|\|\s*", text, maxsplit=1)[0]
        text = re.split(r"\s*[◆♦]\s*", text, maxsplit=1)[0]
        # Very long articles are unsuitable for the e-reader popup. Cut at a
        # natural punctuation boundary after retaining enough room for several
        # meanings rather than keeping pages of examples.
        if len(text) > 1400:
            cut = max(text.rfind(";", 450, 1400), text.rfind(".", 450, 1400))
            text = text[: cut + 1 if cut >= 450 else 1400]
    return clean_definition(text)


def process_legacy_stardict(
    path: Path,
    conn: sqlite3.Connection,
    *,
    source_name: str,
    profile: str = "generic",
    fallback_only: bool = True,
    casefold_aliases: bool,
    yo_aliases: bool,
    accent_aliases: bool,
) -> dict[str, int]:
    fields, idx_raw, dict_raw, syn_raw = load_stardict_bundle(path)
    offset_bits = int(fields.get("idxoffsetbits", "32") or 32)
    entries = parse_stardict_idx(idx_raw, offset_bits=offset_bits)
    sequence = fields.get("sametypesequence", "m")
    added = skipped_existing = rejected = 0
    imported_index_to_lemma: dict[int, str] = {}
    for idx_no, (word, off, size) in enumerate(entries):
        if off + size > len(dict_raw):
            rejected += 1
            continue
        lemma = normalize_legacy_headword(word, profile)
        if not is_lookup_key(lemma):
            rejected += 1
            continue
        if fallback_only and conn.execute("SELECT 1 FROM senses WHERE lemma=? LIMIT 1", (lemma,)).fetchone():
            skipped_existing += 1
            imported_index_to_lemma[idx_no] = lemma
            continue
        article = decode_stardict_article(dict_raw[off:off + size], sequence)
        definition = clean_legacy_definition(lemma, article, profile)
        if not definition:
            rejected += 1
            continue
        if add_sense(conn, lemma, definition, source_name):
            added += 1
        add_link(conn, lemma, lemma, casefold_aliases=casefold_aliases,
                 yo_aliases=yo_aliases, accent_aliases=accent_aliases)
        imported_index_to_lemma[idx_no] = lemma
        if idx_no and idx_no % 5_000 == 0:
            progress_render(source_name, idx_no, len(entries), unit="entries")
        if idx_no and idx_no % 50_000 == 0:
            conn.commit()

    synonyms = 0
    for alias, idx_no in parse_stardict_syn(syn_raw):
        lemma = imported_index_to_lemma.get(idx_no)
        if lemma and is_lookup_key(alias):
            add_link(conn, alias, lemma, casefold_aliases=casefold_aliases,
                     yo_aliases=yo_aliases, accent_aliases=accent_aliases)
            synonyms += 1
    conn.commit()
    if entries:
        progress_finish(source_name, len(entries), len(entries), unit="entries")
    return {
        "source_entries": len(entries),
        "definitions_added": added,
        "skipped_existing": skipped_existing,
        "rejected": rejected,
        "synonyms_added": synonyms,
    }


def _apply_db_pragmas(conn: sqlite3.Connection) -> None:
    tune = sqlite_tuning()
    conn.executescript(
        f"""
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
        PRAGMA cache_size=-{tune['cache_mib'] * 1024};
        PRAGMA mmap_size={tune['mmap_mib'] * 1024 * 1024};
        PRAGMA threads={tune['workers']};
        PRAGMA cache_spill=OFF;
        PRAGMA locking_mode=EXCLUSIVE;
        """
    )


def connect_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, cached_statements=1024)
    _apply_db_pragmas(conn)
    conn.executescript(
        """
        PRAGMA page_size=32768;

        CREATE TABLE senses (
            lemma TEXT NOT NULL,
            definition TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            UNIQUE(lemma, definition)
        );
        CREATE INDEX senses_lemma_idx ON senses(lemma);

        CREATE TABLE links (
            key TEXT NOT NULL,
            lemma TEXT NOT NULL,
            PRIMARY KEY(key, lemma)
        ) WITHOUT ROWID;
        CREATE INDEX links_lemma_idx ON links(lemma);

        CREATE TABLE form_hints (
            key TEXT NOT NULL,
            target TEXT NOT NULL,
            kind TEXT NOT NULL,
            PRIMARY KEY(key, target, kind)
        ) WITHOUT ROWID;
        CREATE INDEX form_hints_target_idx ON form_hints(target);

        CREATE TABLE lookup_overrides (
            key TEXT PRIMARY KEY,
            definition TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT ''
        ) WITHOUT ROWID;
        """
    )
    return conn


def open_existing_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, cached_statements=1024)
    _apply_db_pragmas(conn)
    return conn


def _alias_candidates(key: str, casefold_aliases: bool, yo_aliases: bool, accent_aliases: bool) -> set[str]:
    out = {normalize_key(key)}
    if casefold_aliases:
        out |= {x.casefold() for x in tuple(out)}
    if yo_aliases:
        for x in tuple(out):
            y = yo_alias(x)
            if y:
                out.add(y)
    if accent_aliases:
        for x in tuple(out):
            y = strip_combining_alias(x)
            if y:
                out.add(y)
    return {normalize_key(x) for x in out if is_lookup_key(x)}


def add_link(
    conn: sqlite3.Connection,
    key: str,
    lemma: str,
    *,
    casefold_aliases: bool = True,
    yo_aliases: bool = True,
    accent_aliases: bool = True,
) -> None:
    if not is_lookup_key(key) or not is_lookup_key(lemma):
        return
    lemma = normalize_key(lemma)
    conn.executemany(
        "INSERT OR IGNORE INTO links(key, lemma) VALUES (?, ?)",
        ((alias, lemma) for alias in _alias_candidates(key, casefold_aliases, yo_aliases, accent_aliases)),
    )


def add_form_hint(conn: sqlite3.Connection, key: str, target: str, kind: str) -> None:
    if not is_lookup_key(key) or not is_lookup_key(target) or not kind:
        return
    conn.execute(
        "INSERT OR IGNORE INTO form_hints(key, target, kind) VALUES (?, ?, ?)",
        (normalize_key(key), normalize_key(target), kind),
    )


def has_direct_sense(conn: sqlite3.Connection, lemma: str) -> bool:
    if not is_lookup_key(lemma):
        return False
    return conn.execute(
        "SELECT 1 FROM senses WHERE lemma=? LIMIT 1", (normalize_key(lemma),)
    ).fetchone() is not None


def textual_form_kind(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    low = unicodedata.normalize("NFC", html.unescape(value)).casefold()
    if ("\u0441\u0442\u0440\u0430\u0434" in low or "\u043f\u0430\u0441\u0441\u0438\u0432" in low) and ("\u043f\u0440\u0438\u0447" in low or "participle" in low):
        return "passive_participle"
    return None


def structured_form_kind(sense: dict) -> str | None:
    tags = {str(t).casefold() for t in (sense.get("tags") or [])}
    if "participle" in tags and "passive" in tags:
        return "passive_participle"
    return None


def has_definition_for_key(conn: sqlite3.Connection, key: str) -> bool:
    """Return True when any current lookup alias already resolves to a real sense."""
    if not is_lookup_key(key):
        return False
    candidates = {normalize_key(key), normalize_key(key).casefold()}
    y = yo_alias(normalize_key(key))
    if y:
        candidates.add(y.casefold())
    a = strip_combining_alias(normalize_key(key))
    if a:
        candidates.add(a.casefold())
    for candidate in candidates:
        row = conn.execute(
            "SELECT 1 FROM links l JOIN senses s ON s.lemma=l.lemma WHERE l.key=? LIMIT 1",
            (candidate,),
        ).fetchone()
        if row:
            return True
    return False


def add_sense(conn: sqlite3.Connection, lemma: str, definition: object, source: str) -> bool:
    if not is_lookup_key(lemma):
        return False
    definition = clean_definition(definition)
    if not definition:
        return False
    before = conn.total_changes
    conn.execute(
        "INSERT OR IGNORE INTO senses(lemma, definition, source) VALUES (?, ?, ?)",
        (normalize_key(lemma), definition, source),
    )
    return conn.total_changes > before


class _KaikkiBatch:
    """Bounded bulk writer for the high-volume Wiktextract pass.

    The source parser still makes all filtering/normalization decisions in the
    same order as before.  Only the final SQLite writes are grouped into
    ``executemany`` calls; primary-key/unique constraints remain the authority
    for de-duplication.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        casefold_aliases: bool,
        yo_aliases: bool,
        accent_aliases: bool,
    ) -> None:
        self.conn = conn
        self.casefold_aliases = casefold_aliases
        self.yo_aliases = yo_aliases
        self.accent_aliases = accent_aliases
        self.links: set[tuple[str, str]] = set()
        self.senses: list[tuple[str, str, str]] = []
        self.sense_seen: set[tuple[str, str, str]] = set()
        self.form_hints: set[tuple[str, str, str]] = set()
        self.records = 0

    def link(self, key: str, lemma: str) -> None:
        if not is_lookup_key(key) or not is_lookup_key(lemma):
            return
        lemma = normalize_key(lemma)
        self.links.update(
            (alias, lemma)
            for alias in _alias_candidates(
                key, self.casefold_aliases, self.yo_aliases, self.accent_aliases
            )
        )

    def hint(self, key: str, target: str, kind: str) -> None:
        if not is_lookup_key(key) or not is_lookup_key(target) or not kind:
            return
        self.form_hints.add((normalize_key(key), normalize_key(target), kind))

    def sense(self, lemma: str, definition: object, source: str) -> bool:
        if not is_lookup_key(lemma):
            return False
        definition = clean_definition(definition)
        if not definition:
            return False
        row = (normalize_key(lemma), definition, source)
        if row not in self.sense_seen:
            self.sense_seen.add(row)
            self.senses.append(row)
        # The caller needs to know whether this record carries a lexical sense,
        # not whether another record happened to insert the same row earlier.
        return True

    def should_flush(self) -> bool:
        return (
            self.records >= KAIKKI_BATCH_RECORDS
            or len(self.links) + len(self.senses) + len(self.form_hints) >= KAIKKI_BATCH_ROWS
        )

    def flush(self) -> int:
        inserted_senses = 0
        if self.senses:
            before = self.conn.total_changes
            self.conn.executemany(
                "INSERT OR IGNORE INTO senses(lemma, definition, source) VALUES (?, ?, ?)",
                self.senses,
            )
            inserted_senses = self.conn.total_changes - before
        if self.links:
            self.conn.executemany(
                "INSERT OR IGNORE INTO links(key, lemma) VALUES (?, ?)",
                self.links,
            )
        if self.form_hints:
            self.conn.executemany(
                "INSERT OR IGNORE INTO form_hints(key, target, kind) VALUES (?, ?, ?)",
                self.form_hints,
            )
        self.links.clear()
        self.senses.clear()
        self.sense_seen.clear()
        self.form_hints.clear()
        self.records = 0
        return inserted_senses


def target_words(sense: dict) -> list[str]:
    result: list[str] = []
    for field in ("form_of", "alt_of"):
        value = sense.get(field)
        if not value:
            continue
        if isinstance(value, (str, dict)):
            value = [value]
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    word = item.get("word") or item.get("term")
                else:
                    word = item
                if is_lookup_key(word):
                    result.append(normalize_key(word))
    return result


def sense_is_grammar_only(sense: dict) -> bool:
    tags = {str(t).casefold() for t in (sense.get("tags") or [])}
    return bool(sense.get("form_of") or sense.get("alt_of") or {"form-of", "alt-of"} & tags)


def process_kaikki(
    path: Path,
    conn: sqlite3.Connection,
    langs: set[str],
    *,
    include_forms: bool,
    casefold_aliases: bool,
    yo_aliases: bool,
    accent_aliases: bool,
    max_records: int = 0,
) -> dict[str, int]:
    processed = accepted = definitions = redirects = form_links = 0
    batch = _KaikkiBatch(
        conn,
        casefold_aliases=casefold_aliases,
        yo_aliases=yo_aliases,
        accent_aliases=accent_aliases,
    )
    lang_alt = b"|".join(re.escape(x.encode("ascii", "ignore")) for x in sorted(langs))
    lang_prefilter = re.compile(rb'"lang_code"\s*:\s*"(?:' + lang_alt + rb')"') if lang_alt else None
    with open_jsonl_binary(path) as f:
        for line in f:
            processed += 1
            if max_records and processed > max_records:
                break
            if processed % 25_000 == 0:
                progress_render("Wiktionary", processed, progress_expected("wiktionary_records", processed), unit="records")
            # Flush at a predictable record boundary as well as when the row
            # bound is reached.  This keeps memory bounded on records with many
            # inflected forms and avoids a giant transaction on long dumps.
            if processed % KAIKKI_BATCH_RECORDS == 0:
                definitions += batch.flush()
                conn.commit()
            if lang_prefilter is not None and not lang_prefilter.search(line):
                continue
            try:
                obj = json_loads_fast(line)
            except Exception as exc:
                if not is_json_decode_error(exc):
                    raise
                continue
            if obj.get("lang_code") not in langs:
                continue
            word = obj.get("word")
            if not is_lookup_key(word):
                continue
            word = normalize_key(word)
            accepted += 1
            has_lexical_sense = False
            record_form_hints: list[tuple[str, str]] = []
            for sense in obj.get("senses") or []:
                if not isinstance(sense, dict):
                    continue
                glosses = sense.get("glosses") or []
                if isinstance(glosses, str):
                    glosses = [glosses]
                targets = target_words(sense)
                # Recover aliases from unstructured Russian form-of glosses.
                # This is needed for entries such as:
                #   "Страд. прич. прош. вр. от вкопать"
                textual_target = None
                for gloss in reversed(glosses):
                    textual_target = textual_form_target(gloss)
                    if textual_target:
                        if textual_target not in targets:
                            targets.append(textual_target)
                        break
                form_kind = structured_form_kind(sense)
                if textual_target:
                    form_kind = textual_form_kind(glosses[-1] if glosses else "") or form_kind
                if targets:
                    for target in targets:
                        batch.link(
                            word, target,
                        )
                        if form_kind:
                            batch.hint(word, target, form_kind)
                            record_form_hints.append((target, form_kind))
                        form_links += 1
                if sense_is_grammar_only(sense) or textual_target:
                    continue
                # The leaf gloss is usually the most specific one in Wiktextract.
                if glosses and batch.sense(word, glosses[-1], f"wiktionary:{obj.get('lang_code','')}"):
                    has_lexical_sense = True
            if has_lexical_sense:
                batch.link(
                    word, word,
                )
            redirect = obj.get("redirect")
            if is_lookup_key(redirect):
                batch.link(
                    word, redirect,
                )
                redirects += 1
            if include_forms:
                for form_obj in obj.get("forms") or []:
                    if isinstance(form_obj, dict):
                        form = form_obj.get("form")
                        tags = {str(t).casefold() for t in (form_obj.get("tags") or [])}
                        if tags & {"romanization", "transliteration", "ipa", "audio", "hiragana", "katakana"}:
                            continue
                    else:
                        form = form_obj
                    if is_lookup_key(form):
                        batch.link(
                            form, word,
                        )
                        # When an entry is only a passive-participle redirect, carry
                        # that hint to its inflected forms. If the participle has an
                        # actual lexical sense (e.g. an adjectivized meaning), its
                        # forms must keep that real sense instead.
                        if not has_lexical_sense:
                            for hinted_target, hinted_kind in record_form_hints:
                                batch.hint(form, hinted_target, hinted_kind)
                        form_links += 1
            batch.records += 1
            if batch.should_flush():
                definitions += batch.flush()
    conn.commit()
    definitions += batch.flush()
    conn.commit()
    _PROGRESS.record("wiktionary_records", processed)
    progress_finish("Wiktionary", processed, processed, unit="records")
    return {
        "processed": processed,
        "accepted_language_records": accepted,
        "definitions_added": definitions,
        "form_or_alt_links_seen": form_links,
        "redirects_seen": redirects,
    }


# Wikipedia is used only as a last-resort terminology layer.  The category filter
# deliberately favors concepts, processes, methods, materials, diseases, laws and
# other professional/scientific topics while rejecting biographical/geographic
# encyclopedic noise.  This keeps RU Max Clean a dictionary rather than a tiny
# copy of Wikipedia.
WIKI_CATEGORY_RE = re.compile(
    r"(?:"
    r"физ|математ|хими|биолог|ботан|зоолог|медиц|анатом|физиолог|фарма|ветерин|"
    r"заболев|болезн|синдром|патолог|терап|хирург|диагност|симптом|эпидемиол|"
    r"онколог|кардиол|невролог|педиатр|стоматолог|иммун|генет|биохим|микробиол|"
    r"инженер|техник|технолог|механ|машиностро|электр|электрон|радио|телеком|"
    r"информат|компьют|программ|кибер|строител|архитект|материал|металлург|"
    r"геолог|геодез|картограф|метеоролог|эколог|астроном|косм|авиа|морск|"
    r"судостро|железнодорож|автомоб|транспорт|энергет|нефт|газ|горн|агро|"
    r"сельск|метролог|стандарт|оптик|акуст|робот|автоматик|прибор|"
    r"юрид|право|закон|судеб|уголов|гражданск|административ|конституцион|"
    r"патент|налог|тамож|эконом|финанс|банк|бухгалтер|страх|логист|маркет|"
    r"менедж|статист|лингвист|языкозн|филолог|литературовед|философ|психолог|"
    r"социолог|политолог|военн|оруж|криминал|полиграф|издатель|музык|"
    r"искусствовед|спорт|кулинар|пищев|професс|ремес|производств|свар|токар|"
    r"слесар|электромонтаж|деревообработ|станк|триболог|гидравл|пневмат|"
    r"теплотех|холодил|криоген|лазер|полупровод|микроэлектрон|нанотех|"
    r"квант|ядерн|атомн|спектроскоп|кристаллограф|минералог|петрограф"
    r")",
    re.IGNORECASE,
)
WIKI_NOISE_CATEGORY_RE = re.compile(
    r"(?:"
    r"персонали|родивш|умерш|люди|лауреат|члены|академики|выпускники|преподаватели|"
    r"профессора|учёные по алфавиту|физики|химики|математики|врачи|инженеры|юристы|"
    r"программисты|архитекторы|экономисты|биологи|геологи|лингвисты|политики|спортсмены|акт[ёе]ры|режисс[ёе]ры|музыканты|"
    r"писатели|поэты|журналисты|компании|организации|предприятия|университеты|"
    r"институты|города|сёла|деревни|реки|оз[ёе]ра|районы|станции метро|"
    r"фильмы|телесериалы|альбом|песн|книг|монограф|сборник|игры по алфавиту|"
    r"музыкальн.*групп|рок-групп|музыкальн.*коллектив|видеоигр|компьютерн.*игр|киберспортивн.*команд|"
    r"газеты|журналы|телеканалы|радиостанции|издательства|бренды|торговые марки|"
    r"воинские части|воинские формирования|дивизии|бригады|полки|батальоны|военные операции|"
    r"астероиды|малые планеты|транснептуновые объекты|населённые пункты|деревни|сёла|посёлки|"
    r"музеи|галереи|памятники|торговые центры|отели|просветительские проекты|юбилеи|"
    r"спортивн.*соревнован|спортивн.*сезон|автогон|автоспорт|чемпионат|турнир"
    r")",
    re.IGNORECASE,
)
WIKI_CATEGORY_LINK_RE = re.compile(r"\[\[(?:Категория|Category):([^\]|]+)", re.IGNORECASE)
WIKI_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
WIKI_REF_RE = re.compile(r"<ref\b[^>]*>.*?</ref\s*>|<ref\b[^>]*/\s*>", re.IGNORECASE | re.DOTALL)
WIKI_TAG_RE = re.compile(r"</?(?:small|span|div|sup|sub|br|nowiki|math|chem|code|syntaxhighlight)\b[^>]*>", re.IGNORECASE)
WIKI_EXTERNAL_LINK_RE = re.compile(r"\[(?:https?|ftp)://[^\s\]]+(?:\s+([^\]]+))?\]")
WIKI_CITATION_RE = re.compile(r"\[(?:\d+|уточнить|источник[^\]]*)\]", re.IGNORECASE)
WIKI_BOLD_RE = re.compile(r"'{2,5}")
WIKI_HEADING_RE = re.compile(r"^\s*==", re.MULTILINE)
WIKI_BAD_TITLE_RE = re.compile(
    r"^(?:список|перечень|хронология|дискография|библиография|\d{1,4}\s+год\b|\(\d+\)\s*|\d{1,4}-(?:й|я|е)\b)",
    re.IGNORECASE,
)
WIKI_BAD_TITLE_QUALIFIER_RE = re.compile(
    r"\((?:группа|фильм|альбом|песня|игра|компания|газета|журнал|дивизия|полк|бригада|батальон|корпус|операция|сезон|деревня|село|пос[ёе]лок|город|музей|галерея)\)\s*$",
    re.IGNORECASE,
)
WIKI_ENTITY_LEAD_NOISE_RE = re.compile(
    r"^(?:[А-ЯЁA-Z][^.!?]{0,35}\s+)?(?:рок-группа|музыкальная группа|музыкальный коллектив|поп-группа|"
    r"фильм|телесериал|роман|книга|альбом|песня|компьютерная игра|видеоигра|газета|журнал|"
    r"воинская часть|тактическое соединение|соединение кавалерии|пехотная дивизия|авиационный полк|"
    r"астероид|транснептуновый объект|населённый пункт|деревня|пос[ёе]лок|художественная галерея)\b",
    re.IGNORECASE,
)
WIKI_PERSON_LEAD_RE = re.compile(
    r"(?:\bродил(?:ся|ась)\b|\(\s*\d{1,2}\s+[а-яё]+\s+\d{4}\s*[-—–]|"
    r"—\s*(?:российский|советский|американский|британский|английский|французский|"
    r"немецкий|украинский|белорусский|польский|итальянский|испанский|китайский|"
    r"японский)\s+(?:уч[ёе]ный|физик|химик|математик|врач|юрист|инженер|политик|"
    r"писатель|поэт|акт[ёе]р|режисс[ёе]р|музыкант|спортсмен))",
    re.IGNORECASE,
)


def _strip_nested_markup(text: str, opening: str, closing: str) -> str:
    """Remove balanced MediaWiki template/table blocks without recursion."""
    out: list[str] = []
    i = 0
    depth = 0
    olen, clen = len(opening), len(closing)
    while i < len(text):
        if text.startswith(opening, i):
            depth += 1
            i += olen
            continue
        if depth and text.startswith(closing, i):
            depth -= 1
            i += clen
            continue
        if depth == 0:
            out.append(text[i])
        i += 1
    return "".join(out)


def _wiki_link_text(match: re.Match[str]) -> str:
    target = match.group(1)
    label = match.group(2)
    if target.casefold().startswith(("файл:", "file:", "изображение:", "image:", "категория:", "category:")):
        return ""
    return label if label is not None else target


WIKI_SIMPLE_LINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


def clean_wikipedia_lead(title: str, wikitext: str, *, max_chars: int = 420) -> str:
    """Extract a compact definition-like lead from Russian Wikipedia wikitext."""
    if not wikitext:
        return ""
    text = WIKI_COMMENT_RE.sub(" ", wikitext)
    # Limit expensive markup work to the lead section, but leave enough material
    # to get past infoboxes/templates at the very beginning.
    heading = WIKI_HEADING_RE.search(text)
    if heading:
        text = text[:heading.start()]
    text = _strip_nested_markup(text, "{{", "}}")
    text = _strip_nested_markup(text, "{|", "|}")
    text = WIKI_REF_RE.sub(" ", text)
    text = WIKI_TAG_RE.sub(" ", text)
    text = WIKI_SIMPLE_LINK_RE.sub(_wiki_link_text, text)
    text = WIKI_EXTERNAL_LINK_RE.sub(lambda m: m.group(1) or "", text)
    text = WIKI_CITATION_RE.sub(" ", text)
    text = WIKI_BOLD_RE.sub("", text)
    text = html.unescape(text)
    text = text.replace("\r", "\n")

    paragraphs = [WS_RE.sub(" ", p).strip() for p in re.split(r"\n\s*\n+", text)]
    lead = ""
    for p in paragraphs:
        if len(p) < 30:
            continue
        if p.startswith(("|", "!", "*", "#")):
            continue
        lead = p
        break
    if not lead:
        lead = WS_RE.sub(" ", text).strip()
    if len(lead) < 30 or WIKI_PERSON_LEAD_RE.search(lead):
        return ""

    # Wikipedia normally starts the first sentence with the bold headword.  Remove
    # that repetition so the KOReader card contains only the meaning.
    base_title = re.sub(r"\s*\([^()]*\)\s*$", "", title).strip()
    # The expression above intentionally has a conservative fallback below because
    # article qualifiers can themselves contain punctuation.
    if " (" in title and title.endswith(")"):
        base_title = title.split(" (", 1)[0].strip()
    candidates = [title, base_title]
    stripped = lead
    matched = False
    for head in sorted({x for x in candidates if x}, key=len, reverse=True):
        m = re.match(
            rf"^\s*{re.escape(head)}(?:\s*\([^)]{{0,180}}\))?\s*(?:—|–|-|―|:\s*|—\s*это\s+|\s+—\s+)",
            stripped,
            flags=re.IGNORECASE,
        )
        if m:
            stripped = stripped[m.end():].strip()
            matched = True
            break
        m = re.match(rf"^\s*{re.escape(head)}\s+(?:это|является|представляет собой)\s+", stripped, flags=re.IGNORECASE)
        if m:
            stripped = stripped[m.end():].strip()
            matched = True
            break
    if not matched:
        # A non-definitional first paragraph is not safe to put in a dictionary.
        return ""

    # Wikipedia often inserts pronunciation/translation/acronym expansion before
    # the actual definition. The popup is definition-only, so drop such a leading
    # parenthetical when it is separated by a dash.
    stripped = re.sub(r"^\s*\([^()]{1,180}\)\s*(?:—|–|-)\s*", "", stripped).strip()

    # Keep only the first complete definitional sentence. Encyclopedic background,
    # history, applications and examples belong in Wikipedia, not in the popup.
    sentence_ends = list(re.finditer(r"[.!?](?=\s+[А-ЯЁA-Z]|$)", stripped))
    if sentence_ends:
        stripped = stripped[:sentence_ends[0].end()]
    if len(stripped) > max_chars:
        cut = max(stripped.rfind(". ", 250, max_chars), stripped.rfind("; ", 250, max_chars))
        stripped = stripped[: cut + 1 if cut >= 250 else max_chars].rstrip()
    cleaned = clean_definition(stripped)
    if len(cleaned) < 20:
        return ""
    return cleaned


def _russian_text_values(mapping: object, *, lookup_keys: bool = True) -> list[str]:
    if not isinstance(mapping, dict):
        return []
    out: list[str] = []
    # Prefer the standard Russian spelling, then Russian spelling variants such as
    # ru-x-Q2442696 (pre-1918 orthography).
    keys = sorted(mapping, key=lambda k: (0 if k == "ru" else 1, str(k)))
    for key in keys:
        if key != "ru" and not str(key).startswith("ru-"):
            continue
        item = mapping.get(key)
        value = item.get("value") if isinstance(item, dict) else item
        if lookup_keys:
            if not is_lookup_key(value):
                continue
            value = normalize_key(str(value))
        else:
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                continue
            value = unicodedata.normalize("NFC", value.strip())
        if value not in out:
            out.append(value)
    return out


def process_wikidata_lexemes(
    path: Path,
    conn: sqlite3.Connection,
    *,
    fallback_only: bool,
    include_forms: bool,
    casefold_aliases: bool,
    yo_aliases: bool,
    accent_aliases: bool,
    max_records: int = 0,
) -> dict[str, int]:
    """Import Russian Wikidata Lexeme glosses (CC0) and their written forms."""
    processed = russian = glosses_added = forms_added = skipped_existing = no_ru_gloss = 0
    if str(path).endswith(".bz2"):
        fh_ctx = open_bz2_binary_fast(path)
    elif str(path).endswith(".gz"):
        fh_ctx = open_gzip_binary_fast(path)
    else:
        fh_ctx = open(path, "rb")
    russian_language_marker = re.compile(rb'"language"\s*:\s*"Q7737"')
    with fh_ctx as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            # Wikimedia JSON dumps are arrays. Keep the cheap framing work in
            # bytes so >90% non-Russian lexemes never pay UTF-8 + JSON costs.
            if raw.startswith(b"["):
                raw = raw[1:].lstrip()
            if raw.endswith(b"]"):
                raw = raw[:-1].rstrip()
            if raw.endswith(b","):
                raw = raw[:-1].rstrip()
            if not raw:
                continue
            processed += 1
            if max_records and processed > max_records:
                break
            if processed % 25_000 == 0:
                progress_render("Wikidata Lexemes", processed, progress_expected("wikidata_entities", processed), unit="entities")
            if processed % COMMIT_EVERY == 0:
                conn.commit()
            if not russian_language_marker.search(raw):
                continue
            try:
                obj = json_loads_fast(raw)
            except Exception as exc:
                if not is_json_decode_error(exc):
                    raise
                continue
            if obj.get("type") not in (None, "lexeme") or obj.get("language") != RUSSIAN_LANGUAGE_QID:
                continue
            lemmas = _russian_text_values(obj.get("lemmas"))
            if not lemmas:
                continue
            russian += 1
            lemma = lemmas[0]
            exists = has_definition_for_key(conn, lemma)
            added_here = False
            senses = obj.get("senses")
            if isinstance(senses, dict):
                senses = list(senses.values())
            if not isinstance(senses, list):
                senses = []
            found_gloss = False
            for sense in senses:
                if not isinstance(sense, dict):
                    continue
                glosses = _russian_text_values(sense.get("glosses"), lookup_keys=False)
                if glosses:
                    found_gloss = True
                if fallback_only and exists:
                    continue
                for gloss in glosses:
                    if add_sense(conn, lemma, gloss, "wikidata-lexeme"):
                        glosses_added += 1
                        added_here = True
            if exists and fallback_only:
                skipped_existing += 1
            elif not found_gloss:
                no_ru_gloss += 1

            # Alternative Russian lemmas (including historical spelling variants)
            # are aliases, never separate definition cards.
            if exists or added_here:
                add_link(conn, lemma, lemma, casefold_aliases=casefold_aliases,
                         yo_aliases=yo_aliases, accent_aliases=accent_aliases)
                for alias in lemmas[1:]:
                    add_link(conn, alias, lemma, casefold_aliases=casefold_aliases,
                             yo_aliases=yo_aliases, accent_aliases=accent_aliases)
                if include_forms:
                    forms = obj.get("forms")
                    if isinstance(forms, dict):
                        forms = list(forms.values())
                    if isinstance(forms, list):
                        for form_obj in forms:
                            if not isinstance(form_obj, dict):
                                continue
                            for form in _russian_text_values(form_obj.get("representations")):
                                add_link(conn, form, lemma, casefold_aliases=casefold_aliases,
                                         yo_aliases=yo_aliases, accent_aliases=accent_aliases)
                                forms_added += 1
    conn.commit()
    _PROGRESS.record("wikidata_entities", processed)
    progress_finish("Wikidata Lexemes", processed, processed, unit="entities")
    return {
        "entities_processed": processed,
        "russian_lexemes": russian,
        "glosses_added": glosses_added,
        "forms_linked": forms_added,
        "skipped_existing_lemmas": skipped_existing,
        "without_russian_gloss": no_ru_gloss,
    }


def _defined_lemmas_for_key(conn: sqlite3.Connection, key: str) -> list[str]:
    if not is_lookup_key(key):
        return []
    norm = normalize_key(key)
    candidates = sorted({norm, norm.casefold()})
    placeholders = ",".join("?" for _ in candidates)
    return [r[0] for r in conn.execute(
        f"SELECT DISTINCT l.lemma FROM links l JOIN senses s ON s.lemma=l.lemma WHERE l.key IN ({placeholders}) ORDER BY l.lemma",
        candidates,
    )]


def maybe_upgrade_existing_definition(
    conn: sqlite3.Connection,
    key: str,
    candidate: str,
    *,
    source: str = "ruwiki-quality",
) -> tuple[bool, str]:
    """Replace only an obviously weak single sense with a clearly better candidate."""
    candidate = clean_definition(candidate)
    if not candidate:
        return False, "empty_candidate"
    lemmas = _defined_lemmas_for_key(conn, key)
    if len(lemmas) != 1:
        return False, "ambiguous_key"
    lemma = lemmas[0]
    rows = list(conn.execute(
        "SELECT seq, definition, source FROM senses WHERE lemma=? ORDER BY seq", (lemma,)
    ))
    if len(rows) != 1:
        return False, "multiple_senses"
    seq, old_definition, old_source = rows[0]
    # Do not erase a historical fallback or a user-provided/custom definition.
    if not (str(old_source).startswith("wiktionary:") or str(old_source).startswith("wikidata")):
        return False, "protected_source"
    flags = set(definition_quality_flags(lemma, old_definition, old_source))
    weak = bool(flags & {"very_short", "vague", "early_self_reference", "leading_parenthetical", "old_equivalence_placeholder", "url_residue", "broken_fragment"})
    if not weak:
        return False, "existing_good"
    old_score = definition_quality_score(lemma, old_definition, old_source)
    new_score = definition_quality_score(lemma, candidate, source)
    if new_score < old_score + 12 or new_score < 78:
        return False, "insufficient_gain"
    conn.execute(
        "UPDATE senses SET definition=?, source=? WHERE seq=?",
        (candidate, source, seq),
    )
    return True, f"{old_score}->{new_score}"


WIKI_PREPARED_SCHEMA = 1


def _wiki_prepared_cache_path(path: Path) -> Path | None:
    # Only cache the real rolling Wikimedia source. Tiny test/custom files should
    # remain side-effect free.
    if path.name != "ruwiki-latest-pages-articles.xml.bz2":
        return None
    return path.parent / f"ruwiki-prepared-v{WIKI_PREPARED_SCHEMA}.sqlite3"


def _wiki_source_sig(path: Path) -> tuple[int, int]:
    st = path.stat()
    return int(st.st_size), int(st.st_mtime_ns)


def _wiki_cache_valid(cache_path: Path, source_path: Path) -> bool:
    if not cache_path.exists():
        return False
    try:
        c = sqlite3.connect(cache_path)
        rows = dict(c.execute("SELECT key, value FROM meta"))
        c.close()
        size, mtime = _wiki_source_sig(source_path)
        return (
            int(rows.get("schema", "0")) == WIKI_PREPARED_SCHEMA
            and int(rows.get("source_size", "-1")) == size
            and int(rows.get("source_mtime_ns", "-1")) == mtime
        )
    except Exception:
        return False


def prepare_wikipedia_cache(path: Path) -> Path | None:
    """Extract the expensive 6-GB Wikipedia XML once into a compact candidate cache.

    The cache stores broad professional/scientific candidates before the finer
    quality/noise rules. Therefore later 4.x quality iterations can reuse it without
    re-decompressing the entire bzip2 dump, unless the source dump itself changes.
    """
    cache_path = _wiki_prepared_cache_path(path)
    if cache_path is None:
        return None
    if _wiki_cache_valid(cache_path, path):
        print(f"[КЭШ WIKIPEDIA] Используется подготовленный кэш: {cache_path.name}")
        return cache_path
    tmp = Path(str(cache_path) + ".tmp")
    tmp.unlink(missing_ok=True)
    c = sqlite3.connect(tmp)
    c.executescript("""
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
        PRAGMA page_size=32768;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE candidates (
            title TEXT PRIMARY KEY,
            categories TEXT NOT NULL,
            lead_z BLOB NOT NULL
        ) WITHOUT ROWID;
    """)
    pages = ns0 = broad = 0
    fh_ctx = open_bz2_binary_fast(path) if str(path).endswith(".bz2") else open(path, "rb")
    with fh_ctx as fh:
        for _event, elem in xml_iterparse_end(fh, "page"):
            pages += 1
            if pages % 25_000 == 0:
                progress_render("Wikipedia prepare", pages, progress_expected("wikipedia_pages", pages), unit="pages")
            title = ns = redirect_target = text = None
            for child in list(elem):
                ctag = child.tag.rsplit("}", 1)[-1]
                if ctag == "title":
                    title = child.text or ""
                elif ctag == "ns":
                    ns = child.text
                elif ctag == "redirect":
                    redirect_target = child.attrib.get("title") or ""
                elif ctag == "revision":
                    for sub in child.iter():
                        if sub.tag.rsplit("}", 1)[-1] == "text":
                            text = sub.text or ""
                            break
            if ns != "0" or redirect_target or not is_lookup_key(title) or not text:
                continue
            ns0 += 1
            categories = WIKI_CATEGORY_LINK_RE.findall(text)
            if not categories or not any(WIKI_CATEGORY_RE.search(cat) for cat in categories):
                continue
            title = normalize_key(title)
            # Only the lead is required later; storing it compressed makes the
            # reusable cache much smaller than the original XML dump.
            heading = WIKI_HEADING_RE.search(text)
            lead = text[: heading.start()] if heading else text[:65536]
            lead = lead[:65536]
            c.execute(
                "INSERT OR REPLACE INTO candidates(title, categories, lead_z) VALUES (?, ?, ?)",
                (title, "\n".join(categories), sqlite3.Binary(zlib.compress(lead.encode("utf-8", "replace"), 1))),
            )
            broad += 1
            if broad % 25_000 == 0:
                c.commit()
    c.commit()
    size, mtime = _wiki_source_sig(path)
    meta = {
        "schema": str(WIKI_PREPARED_SCHEMA), "source_size": str(size),
        "source_mtime_ns": str(mtime), "pages": str(pages), "namespace0": str(ns0),
        "broad_candidates": str(broad),
    }
    c.executemany("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", meta.items())
    c.commit(); c.close()
    tmp.replace(cache_path)
    _PROGRESS.record("wikipedia_pages", pages)
    progress_finish("Wikipedia prepare", pages, pages, unit="pages")
    print(f"[КЭШ WIKIPEDIA] Создан {cache_path.name}; кандидатов: {broad:,}".replace(",", " "))
    return cache_path


def _wiki_clean_candidate(row: tuple[str, str, bytes]) -> tuple[bool, str, str, str]:
    """CPU-heavy prepared-Wikipedia cleanup; safe to run in worker processes."""
    title, categories_text, lead_z = row
    if len(title) > 140 or ":" in title or WIKI_BAD_TITLE_RE.search(title) or WIKI_BAD_TITLE_QUALIFIER_RE.search(title):
        return False, title, title, ""
    categories = categories_text.split("\n") if categories_text else []
    if any(WIKI_NOISE_CATEGORY_RE.search(cat) for cat in categories):
        return False, title, title, ""
    base_title = title.split(" (", 1)[0].strip() if " (" in title and title.endswith(")") else title
    try:
        text = zlib.decompress(lead_z).decode("utf-8", "replace")
    except Exception:
        return True, title, base_title, ""
    definition = clean_wikipedia_lead(title, text)
    if definition and WIKI_ENTITY_LEAD_NOISE_RE.search(definition):
        definition = ""
    return True, title, base_title, definition


def _iter_batches(rows, batch_size: int = 512):
    batch = []
    for row in rows:
        batch.append(row)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _wiki_clean_batch(batch):
    return [_wiki_clean_candidate(row) for row in batch]


def _parallel_cleaned_wiki_batches(rows, workers: int):
    """Bounded ProcessPool pipeline so 484k compressed leads are never queued at once."""
    batches = iter(_iter_batches(rows, 512))
    if workers <= 1:
        for batch in batches:
            yield _wiki_clean_batch(batch)
        return
    with ProcessPoolExecutor(max_workers=workers) as pool:
        pending = deque()
        for _ in range(workers * 2):
            try:
                pending.append(pool.submit(_wiki_clean_batch, next(batches)))
            except StopIteration:
                break
        while pending:
            future = pending.popleft()
            yield future.result()
            try:
                pending.append(pool.submit(_wiki_clean_batch, next(batches)))
            except StopIteration:
                pass


def process_wikipedia_prepared(
    prepared: Path,
    conn: sqlite3.Connection,
    *, fallback_only: bool, quality_upgrade_existing: bool,
    casefold_aliases: bool, yo_aliases: bool, accent_aliases: bool,
) -> dict[str, int]:
    pc = sqlite3.connect(prepared)
    meta = dict(pc.execute("SELECT key,value FROM meta"))
    total = pc.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    domain = existing = leads = rejected = upgrades = 0
    done = 0
    cpu = max(1, os.cpu_count() or 1)
    try:
        requested = int(os.environ.get("RU_MAX_WIKI_WORKERS", "0") or 0)
    except ValueError:
        requested = 0
    workers = requested if requested > 0 else max(1, min(8, cpu - 1))
    if total < 50_000:
        workers = 1
    rows = pc.execute("SELECT title,categories,lead_z FROM candidates ORDER BY title COLLATE BINARY")
    for batch in _parallel_cleaned_wiki_batches(rows, workers):
        done += len(batch)
        if done % 5_000 < len(batch):
            progress_render("Wikipedia terms", done, total, unit="candidates")
        for domain_ok, title, base_title, definition in batch:
            if not domain_ok:
                rejected += 1
                continue
            domain += 1
            if not definition:
                rejected += 1
                continue
            title_exists = has_definition_for_key(conn, title)
            base_exists = base_title != title and has_definition_for_key(conn, base_title)
            if fallback_only and (title_exists or base_exists):
                existing += 1
                if quality_upgrade_existing:
                    upgrade_key = title if title_exists else base_title
                    upgraded, _reason = maybe_upgrade_existing_definition(conn, upgrade_key, definition)
                    if upgraded:
                        upgrades += 1
                continue
            if add_sense(conn, title, definition, "ruwiki-lead"):
                add_link(conn, title, title, casefold_aliases=casefold_aliases,
                         yo_aliases=yo_aliases, accent_aliases=accent_aliases)
                if base_title != title and is_lookup_key(base_title):
                    add_link(conn, base_title, title, casefold_aliases=casefold_aliases,
                             yo_aliases=yo_aliases, accent_aliases=accent_aliases)
                leads += 1
            else:
                rejected += 1
        if done % COMMIT_EVERY < len(batch):
            conn.commit()
    pc.close(); conn.commit()
    if total:
        progress_finish("Wikipedia terms", done, total, unit="candidates")
    return {
        "pages_processed": int(meta.get("pages", "0")),
        "namespace0_pages": int(meta.get("namespace0", "0")),
        "domain_candidates": domain,
        "skipped_existing": existing,
        "definitions_added": leads,
        "redirect_aliases_seen": 0,
        "quality_upgrades": upgrades,
        "rejected_or_nondefinitional": rejected,
        "prepared_cache_reused": True,
        "prepared_candidates": total,
        "worker_processes": workers,
    }


def process_wikipedia_terminology(
    path: Path,
    conn: sqlite3.Connection,
    *,
    fallback_only: bool,
    quality_upgrade_existing: bool,
    casefold_aliases: bool,
    yo_aliases: bool,
    accent_aliases: bool,
    max_pages: int = 0,
) -> dict[str, int]:
    """Use Russian Wikipedia leads as fallback definitions for professional terms.

    Only namespace-0 pages in professional/scientific categories are candidates.
    Biographical/geographic/media categories are rejected. This is intentionally
    conservative because a quick dictionary should define terms, not identify every
    person, settlement or work of art in the encyclopedia.
    """
    prepared = prepare_wikipedia_cache(path)
    if prepared is not None:
        return process_wikipedia_prepared(
            prepared, conn, fallback_only=fallback_only,
            quality_upgrade_existing=quality_upgrade_existing,
            casefold_aliases=casefold_aliases, yo_aliases=yo_aliases,
            accent_aliases=accent_aliases,
        )

    pages = ns0 = domain = existing = leads = rejected = redirects = upgrades = 0
    fh_ctx = open_bz2_binary_fast(path) if str(path).endswith(".bz2") else open(path, "rb")
    with fh_ctx as fh:
        for _event, elem in xml_iterparse_end(fh, "page"):
            pages += 1
            if max_pages and pages > max_pages:
                elem.clear()
                break
            if pages % 25_000 == 0:
                progress_render("Wikipedia", pages, progress_expected("wikipedia_pages", pages), unit="pages")
            if pages % COMMIT_EVERY == 0:
                conn.commit()
            title = ns = redirect_target = text = None
            for child in list(elem):
                ctag = child.tag.rsplit("}", 1)[-1]
                if ctag == "title":
                    title = child.text or ""
                elif ctag == "ns":
                    ns = child.text
                elif ctag == "redirect":
                    redirect_target = child.attrib.get("title") or ""
                elif ctag == "revision":
                    for sub in child.iter():
                        if sub.tag.rsplit("}", 1)[-1] == "text":
                            text = sub.text or ""
                            break
            if ns != "0" or not is_lookup_key(title):
                elem.clear()
                continue
            ns0 += 1
            title = normalize_key(title)
            if redirect_target:
                target = normalize_key(str(redirect_target).split("#", 1)[0])
                if is_lookup_key(target) and has_definition_for_key(conn, target):
                    add_link(
                        conn, title, target,
                        casefold_aliases=casefold_aliases,
                        yo_aliases=yo_aliases,
                        accent_aliases=accent_aliases,
                    )
                    redirects += 1
                elem.clear()
                continue
            if not text:
                elem.clear()
                continue
            if len(title) > 140 or ":" in title or WIKI_BAD_TITLE_RE.search(title) or WIKI_BAD_TITLE_QUALIFIER_RE.search(title):
                rejected += 1
                elem.clear()
                continue
            categories = WIKI_CATEGORY_LINK_RE.findall(text)
            if not categories or not any(WIKI_CATEGORY_RE.search(c) for c in categories):
                elem.clear()
                continue
            if any(WIKI_NOISE_CATEGORY_RE.search(c) for c in categories):
                rejected += 1
                elem.clear()
                continue
            domain += 1
            base_title = title.split(" (", 1)[0].strip() if " (" in title and title.endswith(")") else title
            title_exists = has_definition_for_key(conn, title)
            base_exists = base_title != title and has_definition_for_key(conn, base_title)
            definition = ""
            if fallback_only and (title_exists or base_exists):
                existing += 1
                if quality_upgrade_existing:
                    definition = clean_wikipedia_lead(title, text)
                    if definition and WIKI_ENTITY_LEAD_NOISE_RE.search(definition):
                        definition = ""
                    upgrade_key = title if title_exists else base_title
                    upgraded, _reason = maybe_upgrade_existing_definition(conn, upgrade_key, definition)
                    if upgraded:
                        upgrades += 1
                elem.clear()
                continue
            definition = clean_wikipedia_lead(title, text)
            if definition and WIKI_ENTITY_LEAD_NOISE_RE.search(definition):
                definition = ""
            if definition and add_sense(conn, title, definition, "ruwiki-lead"):
                add_link(conn, title, title, casefold_aliases=casefold_aliases,
                         yo_aliases=yo_aliases, accent_aliases=accent_aliases)
                if base_title != title and is_lookup_key(base_title):
                    add_link(conn, base_title, title, casefold_aliases=casefold_aliases,
                             yo_aliases=yo_aliases, accent_aliases=accent_aliases)
                leads += 1
            else:
                rejected += 1
            elem.clear()
    conn.commit()
    _PROGRESS.record("wikipedia_pages", pages)
    if pages:
        progress_finish("Wikipedia", pages, pages, unit="pages")
    return {
        "pages_processed": pages,
        "namespace0_pages": ns0,
        "domain_candidates": domain,
        "skipped_existing": existing,
        "definitions_added": leads,
        "redirect_aliases_seen": redirects,
        "quality_upgrades": upgrades,
        "rejected_or_nondefinitional": rejected,
    }

def process_extra_tsv(
    path: Path,
    conn: sqlite3.Connection,
    *,
    casefold_aliases: bool,
    yo_aliases: bool,
    accent_aliases: bool,
) -> int:
    added = 0
    source = f"extra:{path.name}"
    with path.open("rt", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line or line.lstrip().startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) < 2:
                continue
            word, definition = cols[0].strip(), cols[1].strip()
            if not add_sense(conn, word, definition, source):
                continue
            add_link(
                conn, word, word,
                casefold_aliases=casefold_aliases,
                yo_aliases=yo_aliases,
                accent_aliases=accent_aliases,
            )
            if len(cols) >= 3:
                for alias in cols[2].split("|"):
                    if is_lookup_key(alias):
                        add_link(
                            conn, alias, word,
                            casefold_aliases=casefold_aliases,
                            yo_aliases=yo_aliases,
                            accent_aliases=accent_aliases,
                        )
            added += 1
    conn.commit()
    return added


def process_extra_jsonl(
    path: Path,
    conn: sqlite3.Connection,
    *,
    casefold_aliases: bool,
    yo_aliases: bool,
    accent_aliases: bool,
) -> int:
    """Import JSONL records: {"word":"...","definitions":[...],"aliases":[...]}"""
    added = 0
    source = f"extra:{path.name}"
    with open_jsonl(path) as f:
        for line in f:
            try:
                obj = json_loads_fast(line)
            except Exception as exc:
                if not is_json_decode_error(exc):
                    raise
                continue
            word = obj.get("word")
            if not is_lookup_key(word):
                continue
            defs = obj.get("definitions") or obj.get("glosses") or obj.get("definition") or []
            if isinstance(defs, str):
                defs = [defs]
            any_added = False
            for definition in defs:
                if add_sense(conn, word, definition, source):
                    added += 1
                    any_added = True
            if any_added:
                add_link(
                    conn, word, word,
                    casefold_aliases=casefold_aliases,
                    yo_aliases=yo_aliases,
                    accent_aliases=accent_aliases,
                )
            for alias in obj.get("aliases") or []:
                if is_lookup_key(alias):
                    add_link(
                        conn, alias, word,
                        casefold_aliases=casefold_aliases,
                        yo_aliases=yo_aliases,
                        accent_aliases=accent_aliases,
                    )
    conn.commit()
    return added



def _quality_rewrite_context_parenthetical(lemma: str, text: str, source: str) -> tuple[str, bool]:
    """Turn useful leading parentheses into prose, and discard pure metadata.

    4.5 intentionally left many culturally/historically useful wrappers untouched,
    which is why the real report still had 1.6k ``leading_parenthetical`` rows.
    The goal here is not to erase context but to stop presenting it as a label.
    """
    split = _split_leading_parenthetical(text)
    if not split:
        return text, False
    meta, rest = split
    if not meta or not rest:
        return text, False
    meta_low = meta.casefold()
    rest_low = rest.casefold()

    # Accent/yo/orthographic spelling of the headword in parentheses is display
    # metadata, e.g. "(Боровлево) деревня..." or "(О́льгин) русская фамилия".
    def folded(v: str) -> str:
        v = normalize_key(v).casefold()
        v = unicodedata.normalize("NFD", v)
        v = "".join(ch for ch in v if unicodedata.category(ch) != "Mn")
        return v.replace("ё", "е").strip(" .,:;—–-")
    if folded(meta) == folded(lemma) or (folded(lemma) and folded(lemma) in folded(meta) and len(meta) <= len(lemma) + 12):
        return rest.lstrip(" ,;:—–-"), True

    # Foreign spelling/transliteration wrappers are metadata when a complete
    # Russian semantic class follows: "(Harry) мужское имя ...".  The same rule
    # also handles compact alternate spellings such as "(Цице) река ...".
    letters = sum(ch.isalpha() for ch in meta)
    ascii_letters = sum(ch.isascii() and ch.isalpha() for ch in meta)
    semantic_rest = bool(re.match(
        r"^(?:мужское|женское|личное|русское|испанское|караимское|фамилия|имя|"
        r"река|село|деревня|город|озеро|гора|сопка|термин|доктрина|инструмент|"
        r"танк|реактор|оборудование|компьютер|платформа|устройство)\b",
        rest, re.IGNORECASE,
    ))
    compact_alias = bool(re.fullmatch(r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґ'’.-]+(?:\s+[A-Za-zА-Яа-яЁёІіЇїЄєҐґ'’.-]+)?", meta))
    if semantic_rest and (
        (letters >= 2 and ascii_letters / max(1, letters) >= 0.70)
        or (compact_alias and not meta_low.startswith(("в ", "во ", "у ", "о ", "об ")))
    ):
        return rest.lstrip(" ,;:—–-"), True

    # A comma-separated historical/foreign name list may contain nested
    # parentheses.  If the actual definition that follows clearly identifies an
    # object class, the list is lookup/display metadata rather than the meaning.
    lemma_token = normalize_key(lemma).casefold().strip(" .,:;—–-")
    if lemma_token and len(lemma_token) >= 2 and re.search(
        rf"(?<![а-яёa-z0-9]){re.escape(lemma_token)}(?![а-яёa-z0-9])",
        normalize_key(meta).casefold(), re.IGNORECASE,
    ) and re.search(
        r"\b(?:город|рек|сел|деревн|озер|гор|район|област|провинц|"
        r"государств|царств|фамили|имя)\w*\b", rest[:120], re.IGNORECASE,
    ):
        return rest.lstrip(" ,;:—–-"), True

    # Explicit abbreviation/expansion metadata should become the expansion, not
    # stay as a grammatical label in the popup.
    am = ABBREV_EXPANSION_RE.match(rest)
    if am:
        expanded = _compact_quality_text(am.group(1)).strip(" .;,:—–-")
        if expanded:
            return expanded[0].upper() + expanded[1:], True

    # "(до 1993 г.) название ..." -> "Название ... до 1993 г.".  The date is
    # part of the identity here, but no longer rendered as a leading label.
    hm = LEADING_HISTORICAL_RANGE_RE.match(text)
    if hm:
        context = _compact_quality_text(hm.group("context"))
        body = _compact_quality_text(hm.group("rest")).rstrip(".!? ")
        return f"{body} {context}.", True

    # Current place/entity definitions sometimes carry an old name only as a
    # leading historical note: "(до 1948 года Ойсунки) село ...".  The old name
    # is useful as an alias but not as visible meaning text.  When the remainder
    # itself defines the current object, drop the wrapper.
    if re.match(r"^(?:до|с)\s+(?:1[5-9]\d{2}|20\d{2})\b", meta_low) and re.match(
        r"^(?:село|деревня|город|пос[ёе]лок|река|озеро|гора|район|область)\b",
        rest, re.IGNORECASE,
    ):
        return rest.lstrip(" ,;:—–-"), True

    # Wikipedia frequently exposes expansion/register metadata in parentheses.
    # If the remainder is already a normal definition, simply discard that label.
    if re.match(r"^(?:сокращ|аббревиат|жарг|субстантив|адъектив|предикатив)\w*\.?\b", meta_low):
        return rest.lstrip(" ,;:—–-"), True

    # "(КВ — Клим Ворошилов) опытный советский танк ..." / similar acronym
    # expansions are not meanings themselves.
    if re.search(r"\b[A-ZА-ЯЁ0-9-]{2,12}\s*[-—–:=]\s*", meta) and len(rest) >= 12:
        return rest.lstrip(" ,;:—–-"), True

    # "(расстоянием на множестве) называется однозначная функция ..." is an
    # extraction-shaped definition.  The grammatical frame can be normalized to
    # the actual semantic noun phrase without inventing content.
    nm = re.match(r"^называется\s+(.+)$", rest, re.IGNORECASE | re.DOTALL)
    if nm and len(nm.group(1).strip()) >= 12:
        body = _compact_quality_text(nm.group(1)).strip(" ,;:—–-")
        return body[0].upper() + body[1:], True

    # Pure scientific/domain scopes are metadata.  Keep culturally identifying
    # contexts (mythology, religion, historical geography) because they disambiguate
    # names such as Bastet or Achaea.
    pure_domain = bool(DOMAIN_CONTEXT_ROOTS.search(meta_low)) or bool(re.search(
        r"(?:этолог|фармаколог|зоолог|ботаник|стихослож|метрик|гимнастик|лфк|"
        r"номенклатур|лингвистик|фонетик|морфолог|синтакс)\w*", meta_low
    ))
    if pure_domain:
        return rest.lstrip(" ,;:—–-"), True

    # Selectional restrictions such as "(о растворителе)", "(об обмене)",
    # "(о человеке)" are useful to lexicographers but are still metadata in the
    # Kindle popup.  The headword and the lexical predicate in the remainder carry
    # the meaning, so remove the wrapper rather than displaying it as a label.
    if meta_low.startswith(("о ", "об ", "обо ")) and len(rest) >= 4:
        dependent = rest_low.startswith((
            "молекулы которого", "молекулы которой", "молекулы которых",
            "части которого", "части которой", "части которых",
            "элементы которого", "элементы которой", "элементы которых",
        ))
        if dependent:
            semantic_head = PAREN_DEPENDENT_HEADS.get(meta_low)
            if semantic_head:
                body = f"{semantic_head}, {rest.lstrip(' ,;:—–-')}"
                return body, True
        else:
            return rest.lstrip(" ,;:—–-"), True

    # Usage-frame metadata can usually be dropped outright.
    if re.match(r"^(?:обычно|только|преим(?:ущественно)?|в\s+речи|с\s+оттенком|редко|"
                r"уголовн(?:ый|ая)\s+сленг|советск\.?|разг\.?|прост\.?|устар\.?|бранн\.?|"
                r"перен\.?|книжн\.?|поэт\.?|ирон\.?|неодобр\.?)\b", meta_low):
        return rest.lstrip(" ,;:—–-"), True

    # Government/object slot metadata belongs in the sentence, not as a label:
    # "(кого-либо) подвергать ампутации" -> "Подвергать кого-либо ампутации".
    if re.fullmatch(r"(?:кого|кому|кем|чего|чему|чем|кого-либо|кому-либо|кем-либо|чего-либо|чему-либо|чем-либо)", meta_low):
        words = rest.split(None, 1)
        if words and re.match(r"^[А-Яа-яЁё-]+(?:ть|ти|чь|ет|ит|ать|ять)$", words[0], re.IGNORECASE):
            body = words[0] + " " + meta + ((" " + words[1]) if len(words) > 1 else "")
            return body[0].upper() + body[1:], True

    # Keep genuinely semantic adjectives from the wrapper as ordinary prose.
    if re.fullmatch(r"(?:врожд[ёе]нн(?:ый|ая|ое)|съ[ёе]мн(?:ый|ая|ое)|полностью)", meta_low):
        body = f"{meta} {rest}".strip()
        return body[0].upper() + body[1:], True

    # Prefix notation such as "(радио-)передатчик" is a word-building note; the
    # clean meaning is the joined lexical item.
    if meta.endswith("-") and re.fullmatch(r"[A-Za-zА-Яа-яЁё-]{2,40}-", meta) and re.match(r"^[A-Za-zА-Яа-яЁё]", rest):
        body = meta[:-1] + rest
        return body[0].upper() + body[1:], True

    # Foreign-script/etymological spellings are metadata when a real definition
    # follows.  This catches Hebrew/Greek/Chinese notes that do not trip the ASCII
    # transliteration heuristic above.
    if re.match(
        r"^(?:ивр(?:ит)?|греч(?:еск)?|лат(?:инск)?|англ(?:ийск)?|нем(?:ецк)?|фр(?:анц)?|"
        r"исп(?:анск)?|кит(?:айск)?|яп(?:онск)?|санскритск|инг\.|чеч\.)\b", meta_low
    ):
        return rest.lstrip(" ,;:—–-"), True

    # Speculative spelling notes from Dal/Wiktionary are not the meaning.
    if meta_low.startswith(("вероятно ", "возможно ")) and len(rest) >= 8:
        return rest.lstrip(" ,;:—–-"), True
    if meta_low.startswith("говорят ") and rest_low.startswith("или "):
        body = rest[4:].lstrip(" ,;:—–-")
        return (body[0].upper() + body[1:]) if body else rest, True
    if meta_low in {"санскритский термин", "геометрическое представление"} and len(rest) >= 8:
        return rest.lstrip(" ,;:—–-"), True

    # "(при печатании тканей) прибор ..." keeps the condition as normal prose.
    if meta_low.startswith("при ") and len(rest) >= 8:
        context = meta[4:].strip()
        body = rest.rstrip(".!? ")
        if context and context.casefold() not in body.casefold():
            body = f"{body} при {context}"
        return body.rstrip(".!? ") + ("." if source.startswith("ruwiki") else ""), True

    # Cultural/historical location: preserve it as ordinary prose at the end.
    if meta_low.startswith(("в ", "во ", "у ", "согласно ")):
        body = rest.rstrip(".!? ")
        # Avoid duplicating the same context when the body already contains it.
        if meta_low not in body.casefold():
            body = f"{body} {meta}"
        return body.rstrip(".!? ") + ("." if source.startswith("ruwiki") else ""), True

    # Adjectival semantic context should become prose instead of a label.
    if meta_low in {"историческая", "исторический", "историческое", "высокий", "низкий"}:
        body = f"{meta} {rest}".strip()
        return body[0].upper() + body[1:], True

    # A parenthesized semantic noun phrase followed by a dangling participle is
    # better as one normal sentence: "(патологический рефлекс) проявляющийся...".
    if BAD_REMAINDER_START_RE.match(rest) and not meta_low.startswith(("с ", "от ", "для ")):
        body = f"{meta}, {rest}"
        return body[0].upper() + body[1:], True

    # "(персонаж ... мифологии) дочь Эола" -> ordinary semantic prose.
    if "мифолог" in meta_low or "религи" in meta_low or "фольклор" in meta_low or "эпос" in meta_low:
        body = rest.rstrip(".!? ") + ", " + meta
        return body[0].upper() + body[1:] + ("." if source.startswith("ruwiki") else ""), True

    return text, False


def _quality_strip_leading_metadata(text: str, source: str) -> tuple[str, bool]:
    """Strip non-semantic leading parentheses without deleting real meaning.

    The source parsers already remove classic domain labels.  This second pass
    targets metadata that surfaced in the full QA report: etymology/translation,
    grammatical gender, model-index explanations, and mythology-domain wrappers.
    Wikipedia parentheticals are only stripped when the remainder still looks like
    a self-contained definition rather than a dangling participial clause.
    """
    original = text
    text = strip_leading_context_parenthetical(text)
    changed = text != original
    for _ in range(2):
        split = _split_leading_parenthetical(text)
        if not split:
            break
        meta, rest = split
        meta_low = meta.casefold()
        remove = bool(META_PAREN_CONTENT_RE.search(meta_low))
        if not remove and re.match(r'^[«"“„].{1,100}[»"”](?:\s*[сc])?$', meta):
            remove = True
        if not remove and re.match(r'^(?:HMS|USS|SMS|HMAS|RMS)\b', meta, re.IGNORECASE):
            remove = True
        # Acronym expansions such as "(non return to zero) код ..." are metadata,
        # not the meaning itself.  Require a sufficiently long, non-dangling rest.
        ascii_letters = sum(ch.isascii() and ch.isalpha() for ch in meta)
        letters = sum(ch.isalpha() for ch in meta)
        if (
            not remove and source != "dal" and letters >= 2
            and ascii_letters / max(1, letters) >= 0.75 and len(rest) >= 20
            and not BAD_REMAINDER_START_RE.match(rest)
        ):
            remove = True
        if not remove:
            break
        if BAD_REMAINDER_START_RE.match(rest):
            # Removing the parenthetical would leave "выпущенный...", "известный..."
            # and similar non-definitions. Keep it for the later QA/rejection step.
            break
        text = rest.lstrip(" ,;:—–-")
        changed = True
    return text, changed


def _quality_normalize_definition(lemma: str, definition: str, source: str) -> tuple[str, set[str]]:
    """Normalize an already parsed definition and report which cleanup rules fired."""
    changes: set[str] = set()
    text = unicodedata.normalize("NFC", str(definition or "")).strip()
    if not text:
        return "", changes

    newer = EXAMPLE_TAIL_RE.sub("", text).rstrip()
    if newer != text:
        text = newer
        changes.add("example_tail_removed")
    newer = NKRJA_TAIL_RE.sub("", text).rstrip()
    if newer != text:
        text = newer
        changes.add("corpus_tail_removed")
    newer = INLINE_SENSE_REF_RE.sub("", text)
    if newer != text:
        text = newer
        changes.add("sense_reference_removed")
    newer = INLINE_CITATION_RE.sub("", text)
    if newer != text:
        text = newer
        changes.add("citation_removed")
    if URL_RE.search(text):
        newer = URL_RE.sub("", text).strip(" ,;:-—–")
        if newer != text:
            text = newer
            changes.add("url_removed")

    # Exact ``Headword (descriptor)`` rows are common in Wiktionary.  When the
    # parenthetical itself is a semantic class ("фамилия", "раздел книги" ...),
    # keep that class and drop the redundant headword.  Do not touch arbitrary
    # parentheticals such as pronunciation/orthography or "(аналогично ...)".
    tm = TRAILING_HEADWORD_DESCRIPTOR_RE.fullmatch(text)
    if tm:
        head = _compact_quality_text(tm.group("head"))
        desc = _compact_quality_text(tm.group("desc"))
        if normalize_key(head).casefold() == normalize_key(lemma).casefold() and TRAILING_DESCRIPTOR_NOUN_RE.search(desc):
            text = desc[0].upper() + desc[1:]
            changes.add("trailing_descriptor_rewritten")

    # Dal StarDict occasionally concatenates the next alphabetic headword into the
    # same article.  A new capitalized token immediately followed by a gender
    # marker after sentence punctuation is a strong structural boundary.
    if source == "dal" and len(text) >= 80:
        m = DAL_NEXT_HEADWORD_RE.search(text)
        if m and m.start() >= 35:
            text = text[:m.start()].rstrip()
            changes.add("dal_cluster_trimmed")

    # Remove a repeated headword or Wikipedia-style copula from the beginning.
    heads = {lemma}
    if " (" in lemma and lemma.endswith(")"):
        heads.add(lemma.split(" (", 1)[0].strip())
    for head in sorted((h for h in heads if h), key=len, reverse=True):
        patterns = (
            # A dash/colon and "X это Y" normally leave Y in nominative case and
            # are safe to remove. Do NOT strip "X является Y" or
            # "X представляет собой Y": the remainder is often instrumental or
            # accusative (e.g. "GiNaC является C++ библиотекой").
            rf"^\s*{re.escape(head)}\s*(?:—|–|-|:)\s*",
            rf"^\s*{re.escape(head)}\s+это\s+",
        )
        hit = False
        for pat in patterns:
            m = re.match(pat, text, re.IGNORECASE)
            if m:
                text = text[m.end():].lstrip()
                changes.add("headword_prefix_removed")
                hit = True
                break
        if hit:
            break
    m = re.match(r"^\s*это\s+", text, re.IGNORECASE)
    if m and len(text[m.end():].strip()) >= 12:
        text = text[m.end():].lstrip()
        changes.add("empty_copula_removed")

    # First convert useful parenthesized context to ordinary prose.  Then run the
    # stricter metadata stripper for etymology/domain/transliteration wrappers.
    text2, context_rewritten = _quality_rewrite_context_parenthetical(lemma, text, source)
    if context_rewritten:
        text = text2
        changes.add("leading_context_rewritten")
    text2, stripped_meta = _quality_strip_leading_metadata(text, source)
    if stripped_meta:
        text = text2
        changes.add("leading_metadata_removed")
        # Parenthetical removal can expose ordinary register labels.
        text = strip_leading_labels(text)

    # Context/metadata removal can expose "это ..." or "сокр. от ..." after the
    # first copula pass, so normalize once more here.
    m = re.match(r"^\s*это\s+", text, re.IGNORECASE)
    if m and len(text[m.end():].strip()) >= 8:
        text = text[m.end():].lstrip()
        changes.add("empty_copula_removed")
    am = ABBREV_EXPANSION_RE.match(text)
    if am:
        expanded = _compact_quality_text(am.group(1)).strip(" .;,:—–-")
        if expanded:
            text = expanded
            changes.add("abbreviation_expanded")

    text2, about_rewritten = _rewrite_about_fragment(text)
    if about_rewritten:
        text = text2
        changes.add("about_fragment_rewritten")

    if source == "dal":
        before = text
        for _ in range(3):
            newer = DAL_LEADING_REGION_RE.sub("", text, count=1)
            newer = DAL_LEADING_OLD_USAGE_RE.sub("", newer, count=1)
            if newer == text:
                break
            text = newer.lstrip(" ,;:—–-")
        # A leading question-mark parenthesis in Dal is normally speculative
        # etymology, not the meaning (e.g. "(ах? охапка?) ...").
        m = LEADING_META_PAREN_RE.match(text)
        if m and "?" in m.group("meta") and len(m.group("rest").strip()) >= 16:
            text = m.group("rest").lstrip(" ,;:—–-")
        if text != before:
            changes.add("leading_metadata_removed")
        compact_dal = _dal_compact_long_definition(lemma, text)
        if compact_dal != text:
            text = compact_dal
            changes.add("dal_long_compacted")

    text = WS_RE.sub(" ", text).strip(" \t\r\n;,•")
    text = clean_definition(text)
    if text and BROKEN_MEANINGLESS_STUB_RE.fullmatch(text.strip()):
        changes.add("broken_stub_removed")
        return "", changes
    if text and ((text.startswith("[") and "]" not in text) or text in {"Над ()", "() овощ"}):
        changes.add("broken_stub_removed")
        return "", changes
    text2, bad_residue_removed = _strip_bad_residue(text)
    if bad_residue_removed:
        text = text2
        changes.add("bad_residue_removed")
        # Residue removal can expose a grammar-only form gloss that was hidden
        # behind a trailing bullet (e.g. ``Страд. прич. ... ·``). Re-run the
        # existing conservative cleaner once; it returns an empty definition for
        # unmistakable textual form-of records.
        text = clean_definition(text)
    if not text:
        return "", changes

    if re.fullmatch(r"\(\s*(?:о|об|обо|про|при|для)\s+[^()]{1,100}\s*\)", text, re.IGNORECASE):
        changes.add("nondefinition_removed")
        return "", changes

    if EMPTY_META_DEFINITION_RE.fullmatch(text) or FRAGMENT_DEFINITION_RE.fullmatch(text):
        changes.add("nondefinition_removed")
        return "", changes

    # A malformed Wiktionary gloss from the QA report looked like
    # "О расстреле Белого дома ... ◆ examples".  If a definition starts with the
    # preposition "О" and immediately repeats its own headword, it is prose about
    # the entry rather than a meaning.
    low = text.casefold()
    lemma_low = normalize_key(lemma).casefold()
    if source.startswith("wiktionary") and (
        ABOUT_PREAMBLE_RE.match(text)
        or (low.startswith("о ") and lemma_low and lemma_low in low[:100])
    ):
        changes.add("nondefinition_removed")
        return "", changes

    if source.startswith("ruwiki") and WIKI_LIST_RESIDUE_RE.search(text):
        changes.add("wikipedia_list_residue_removed")
        return "", changes

    if source.startswith("ruwiki") and (
        WIKI_NAMED_SHIP_RE.search(lemma) or WIKI_POST_ENTITY_NOISE_RE.search(text[:220])
    ):
        eventish = bool(re.search(r"\b(?:гонка|раунд|этап|чемпионат)\b", text[:180], re.IGNORECASE))
        if WIKI_NAMED_SHIP_RE.search(lemma) or (not eventish) or WIKI_EVENT_TITLE_RE.search(lemma):
            changes.add("wikipedia_entity_noise_removed")
            return "", changes

    if source.startswith("ruwiki"):
        # Remove a few unmistakable extraction artefacts before judging the lead.
        newer = WIKI_STRAY_MEDIA_TAIL_RE.sub("", text).rstrip(" ,;:-—–")
        if newer != text and len(newer) >= 12:
            text = newer
            changes.add("wikipedia_broken_tail_removed")
        newer = re.sub(r"\s*\(\s*см\.?\s*$", "", text, flags=re.IGNORECASE).rstrip(" ,;:-—–")
        if newer != text and len(newer) >= 12:
            text = newer
            changes.add("wikipedia_broken_tail_removed")

        # A leading parenthetical followed only by a dangling participle is not a
        # dictionary definition (Fairlight CMI in the real 4.5 report).
        ps = _split_leading_parenthetical(text)
        if ps and BAD_REMAINDER_START_RE.match(_compact_quality_text(ps[1])):
            changes.add("wikipedia_broken_fragment_removed")
            return "", changes

        # Old 4.3/4.4 max caches may contain two-sentence Wikipedia leads. The
        # first complete sentence is the dictionary definition; later sentences
        # are normally history, applications or examples.
        sentence_end = re.search(r"[.!?](?=\s+[А-ЯЁA-Z0-9]|$)", text)
        if sentence_end and sentence_end.end() < len(text):
            text = text[:sentence_end.end()].rstrip()
            changes.add("wikipedia_extra_sentences_removed")

        # Dated creation/adoption/manufacturing history is not part of lexical
        # meaning when an informative object class already precedes the clause.
        # 4.5 required a 24-character/3-word core and therefore missed simple but
        # good classes such as "Крупнокалиберный патрон" and "Программная платформа".
        years_in_text = _historical_years(text)
        title_years = _historical_years(lemma)
        if years_in_text and not years_in_text.issubset(title_years):
            hist = WIKI_HISTORY_CLAUSE_RE.search(text)
            if hist is None:
                hist = WIKI_HISTORY_INLINE_RE.search(text)
            if hist:
                core = text[:hist.start()].rstrip(" ,;:-—–")
                core_words = core.casefold().rstrip(".!?").split()
                generic = " ".join(core_words) in GENERIC_WIKI_CORES
                # Named technical/product entities are still meaningfully defined
                # by a compact class ("Audi Q5 -> Кроссовер", "ASCI White ->
                # Суперкомпьютер").  4.6 left their manufacturing dates visible
                # because a one-word class was considered too generic.  For a
                # model-like title, the class itself is preferable to history.
                model_like_title = bool(re.search(r"[A-Za-z0-9]|[А-ЯЁ]{2,}[-0-9]", lemma))
                informative_core = (len(core) >= 12 and len(core_words) >= 2 and not generic)
                compact_named_core = (model_like_title and len(core) >= 6 and len(core_words) >= 1)
                if informative_core or compact_named_core:
                    text = core.rstrip(".!?") + "."
                    changes.add("wikipedia_history_tail_removed")

        # 4.8 named-entity core: when a Wikipedia lead is plainly an individual
        # product/institution/object and begins with a compact class, discard the
        # dated biography/manufacturing clause even when older history regexes miss
        # its exact wording. This is deliberately limited to model/proper-name
        # headwords and a whitelist of object classes.
        if _historical_years(text) and re.search(r"[A-ZА-ЯЁ0-9]", lemma):
            cm = re.match(r"^(?P<core>(?:суперкомпьютер|микрокомпьютер|автомобиль|кроссовер|седан|пикап|"
                          r"электромобиль(?:-[а-яё-]+)?|минивэн|микровэн|фургон|родстер|купе|тарга|"
                          r"концепт-кар|электробус|автобус|двигатель))\b", text, re.IGNORECASE)
            if cm:
                core = cm.group("core").strip()
                # Preserve useful type modifiers already inside the class token,
                # but not company/date history following it.
                if 4 <= len(core) <= 40:
                    text = core[0].upper() + core[1:] + "."
                    changes.add("wikipedia_named_core_compacted")

        # Remaining dangling extraction tails are too broken for a meaning-only
        # popup; reject them so an exact-title rescue can try a better source row.
        if WIKI_DANGLING_TAIL_RE.search(text) or abs(text.count("(") - text.count(")")) >= 2:
            changes.add("wikipedia_broken_fragment_removed")
            return "", changes

    if text and text[0].isalpha() and text[0] == text[0].lower():
        text = text[0].upper() + text[1:]
    return text, changes


def _defined_lemmas_for_key(conn: sqlite3.Connection, key: str) -> list[str]:
    """Return canonical lemmas currently reachable from a lookup spelling/form."""
    if not is_lookup_key(key):
        return []
    out: set[str] = set()
    for candidate in _alias_candidates(key, True, True, True):
        for (lemma,) in conn.execute(
            "SELECT DISTINCT l.lemma FROM links l JOIN senses s ON s.lemma=l.lemma WHERE l.key=?",
            (candidate,),
        ):
            out.add(lemma)
        if conn.execute("SELECT 1 FROM senses WHERE lemma=? LIMIT 1", (candidate,)).fetchone():
            out.add(candidate)
    return sorted(out, key=lambda x: (x.casefold(), x))


def _clean_alias_candidate(value: str) -> str:
    value = _compact_quality_text(value).strip(" .;,:—–-")
    value = re.sub(r"[ᴵᴵᴵᐟ¹²³⁴⁵⁶⁷⁸⁹⁰]+$", "", value).strip()
    value = ALIAS_POS_PREFIX_RE.sub("", value).strip(" .;,:—–-")
    value = re.sub(r"^(?:слово|слова|имя|имени|название)\s+", "", value, flags=re.IGNORECASE).strip()
    return value


def _alias_fallback_definition(parsed: tuple[str, list[str], str, str]) -> str | None:
    kind, _candidates, tail, body = parsed
    fallback = tail.strip(" .;,:—–-") if tail else ""
    if not fallback and kind.startswith("то же"):
        # When the target article is absent, a long "то же, что ..." body can
        # still contain the actual dictionary meaning (e.g. an abbreviation
        # expansion).  Strip the redirect prose instead of exposing it verbatim.
        probe = body.strip(" .;,:—–-")
        if len(probe) >= 12 and (len(probe.split()) >= 2 or "," in probe):
            fallback = probe
    if not fallback:
        return None
    fallback = _compact_quality_text(fallback)
    if not fallback or EMPTY_META_DEFINITION_RE.fullmatch(fallback) or FRAGMENT_DEFINITION_RE.fullmatch(fallback):
        return None
    if fallback[0].isalpha() and fallback[0] == fallback[0].lower():
        fallback = fallback[0].upper() + fallback[1:]
    return fallback


def _quality_alias_resolution(
    conn: sqlite3.Connection, lemma: str, definition: str, source: str
) -> tuple[list[str], str | None] | None:
    """Resolve one explicit textual alias sense into canonical article targets.

    Unlike 4.6, an alias sense may be removed even when the same headword has
    other real senses.  The lookup key then legitimately points to both its own
    article and the aliased article, which is exactly how StarDict ambiguity is
    represented.  This removes thousands of visible "То же, что ..." lines.
    """
    parsed = _parse_alias_formula(definition)
    if not parsed and source.startswith(("wiktionary:cu", "wiktionary:orv", "wiktionary:ru-old")):
        m = SHORT_ALIAS_RE.fullmatch(_compact_quality_text(definition))
        if m:
            target = normalize_key(m.group(1))
            if target and target.casefold() != normalize_key(lemma).casefold():
                parsed = ("historical-bare", [target], "", target)
    if not parsed:
        return None
    _kind, candidates, _tail, _body = parsed
    current = normalize_key(lemma)
    targets: set[str] = set()
    for raw in candidates:
        candidate = _clean_alias_candidate(raw)
        if not candidate:
            continue
        variants = [candidate]
        if " (" in candidate and candidate.endswith(")"):
            variants.append(candidate.split(" (", 1)[0].strip())
        for variant in variants:
            for canonical in _defined_lemmas_for_key(conn, variant):
                if normalize_key(canonical) != current:
                    targets.add(canonical)
    return sorted(targets, key=lambda x: (x.casefold(), x)), _alias_fallback_definition(parsed)


def _quality_about_targets(conn: sqlite3.Connection, lemma: str, definition: str) -> list[str]:
    """Resolve short Wiktionary glosses like ``О коте`` through morphology.

    This avoids trying to invent Russian nominative forms.  The existing lookup
    graph already knows that ``коте`` resolves to ``кот``, ``Кубе`` to ``Куба``
    and so on.  Only an actually resolvable referent is converted to an alias.
    """
    text = _compact_quality_text(definition)
    if len(text) > 100:
        return []
    m = ABOUT_FRAGMENT_RE.fullmatch(text)
    if not m:
        return []
    body = _compact_quality_text(m.group(1)).strip(" .;:—–-")
    if not body or body.casefold().startswith(("том,", "том ", "чём-либо", "чем-либо", "ком-либо")):
        return []
    candidates = [body]
    cm = ABOUT_SAFE_CLASS_PREFIX_RE.match(body)
    if cm and len(cm.group(1).split()) <= 4:
        candidates.append(cm.group(1).strip(" «»\"'"))
    current = normalize_key(lemma)
    out: set[str] = set()
    for candidate in candidates:
        candidate = candidate.strip(" «»\"'")
        if not is_lookup_key(candidate):
            continue
        for canonical in _defined_lemmas_for_key(conn, candidate):
            if normalize_key(canonical) != current:
                out.add(canonical)
    return sorted(out, key=lambda x: (x.casefold(), x))



def _migrate_dal_headwords(
    conn: sqlite3.Connection,
    *,
    casefold_aliases: bool,
    yo_aliases: bool,
    accent_aliases: bool,
) -> tuple[int, int]:
    """Normalize POS-contaminated Dal lemmas already present in older max caches.

    4.4 could import keys such as ``азбука ж`` because some legacy StarDict
    indexes embed the gender marker in the headword.  Fixing this in the active
    quality stage preserves compatibility with an existing 4.4 max cache: no
    expensive Wiktionary/Wikidata/Wikipedia reparse is required.
    """
    normalized = conflicts_removed = 0
    rows = conn.execute("SELECT DISTINCT lemma FROM senses WHERE source='dal' ORDER BY lemma").fetchall()
    for (old_lemma,) in rows:
        target = DAL_HEADWORD_POS_SUFFIX_RE.sub("", normalize_key(old_lemma)).strip()
        if target == old_lemma or not is_lookup_key(target):
            continue

        # Preserve real synonym keys that pointed to the old Dal article, but do
        # not preserve the metadata-contaminated headword itself.
        alias_keys = [r[0] for r in conn.execute("SELECT key FROM links WHERE lemma=?", (old_lemma,)).fetchall()]
        target_exists = conn.execute(
            "SELECT 1 FROM senses WHERE lemma=? AND lemma<>? LIMIT 1", (target, old_lemma)
        ).fetchone()
        if target_exists:
            # Dal is a fallback layer. If the cleaned lemma is already defined by
            # a better source, drop the duplicate Dal article and keep only useful
            # aliases/synonyms.
            conn.execute("DELETE FROM senses WHERE lemma=? AND source='dal'", (old_lemma,))
            conflicts_removed += 1
        else:
            conn.execute("UPDATE senses SET lemma=? WHERE lemma=? AND source='dal'", (target, old_lemma))
            normalized += 1

        conn.execute("DELETE FROM links WHERE lemma=?", (old_lemma,))
        add_link(
            conn, target, target,
            casefold_aliases=casefold_aliases, yo_aliases=yo_aliases, accent_aliases=accent_aliases,
        )
        for alias in alias_keys:
            if alias == old_lemma or DAL_HEADWORD_POS_SUFFIX_RE.search(alias):
                continue
            if is_lookup_key(alias):
                add_link(
                    conn, alias, target,
                    casefold_aliases=casefold_aliases, yo_aliases=yo_aliases, accent_aliases=accent_aliases,
                )
    return normalized, conflicts_removed


def _semantic_candidate_rows(conn: sqlite3.Connection) -> list[tuple[int, str, str, str]]:
    """Return only senses that can trigger the active 4.6 normalization rules.

    4.5 normalized every ~600k definition even though fewer than 6% were changed.
    SQLite can cheaply preselect all Dal/Wikipedia rows plus suspicious/short
    Wiktionary rows.  The wide conditions intentionally prefer false positives to
    false negatives; skipping a candidate is never allowed to change semantics.
    """
    return list(conn.execute(
        """
        SELECT seq, lemma, definition, source
        FROM senses
        WHERE source='dal'
           OR source LIKE 'ruwiki%'
           OR source IN ('wiktionary:cu','wiktionary:orv','wiktionary:ru-old')
           OR length(definition) <= 40
           OR length(definition) > 520
           OR substr(ltrim(definition),1,1) IN ('(', '[')
           OR instr(definition, '◆') > 0
           OR instr(definition, '◇') > 0
           OR instr(definition, '[НКРЯ]') > 0
           OR instr(definition, '[') > 0
           OR instr(definition, '{{') > 0
           OR instr(definition, '[[') > 0
           OR instr(definition, ' * ') > 0
           OR instr(lower(definition), 'http://') > 0
           OR instr(lower(definition), 'https://') > 0
           OR substr(ltrim(definition),1,8) IN ('Сокр. от','сокр. от','Аббр. от','аббр. от','То же, ч','то же, ч')
           OR substr(ltrim(definition),1,4) IN ('Это ','это ','См. ','см. ','Обо ','обо ')
           OR substr(ltrim(definition),1,2) IN ('К ','к ','О ','о ')
           OR substr(ltrim(definition),1,3) IN ('Об ','об ')
           OR substr(ltrim(definition),1,8) IN ('Вариант ','вариант ')
           OR substr(ltrim(definition),1,7) IN ('Уменьш.','уменьш.','Гипокор','гипокор')
           OR substr(ltrim(definition),1,6) IN ('Димин.','димин.')
           OR substr(ltrim(definition),1,8) IN ('Свойство','свойство','Явление','явление')
           OR substr(ltrim(definition),1,7) IN ('Процесс','процесс')
           OR substr(ltrim(definition),1,11) IN ('Способность','способность')
           -- 4.8.4: ensure the conservative bad-residue scrub sees ordinary
           -- Wiktionary rows even when their definitions are longer than the
           -- legacy short-text candidate window.
           OR substr(ltrim(definition),1,1) IN ('·', '•')
           OR instr(definition, '()') > 0
           OR instr(definition, '[]') > 0
           OR instr(definition, '|| :') > 0
           OR instr(definition, '; :') > 0
           OR instr(definition, ', :') > 0
           OR instr(definition, '| :') > 0
           OR substr(rtrim(definition), -1, 1) IN (':', '"', '»', '“', '”', '’')
           OR substr(ltrim(definition),1,6) IN ('Страд.','страд.')
           OR substr(definition,1,length(lemma)) = lemma
        ORDER BY seq
        """
    ))


def _quality_normalize_batch(rows: list[tuple[int, str, str, str]]) -> list[tuple[int, str, str, str, str, tuple[str, ...]]]:
    out: list[tuple[int, str, str, str, str, tuple[str, ...]]] = []
    for seq, lemma, definition, source in rows:
        newdef, changes = _quality_normalize_definition(lemma, definition, source)
        out.append((seq, lemma, definition, source, newdef, tuple(sorted(changes))))
    return out


def _batched_rows(rows: list[tuple[int, str, str, str]], size: int = 2500):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def semantic_quality_pass(
    conn: sqlite3.Connection,
    *,
    casefold_aliases: bool = True,
    yo_aliases: bool = True,
    accent_aliases: bool = True,
) -> dict[str, int]:
    """Actively clean known non-semantic residue before link resolution.

    4.6 is candidate-driven and CPU-parallel.  The old 4.5 pass normalized every
    definition serially (~2m17s on the user's 16-thread machine) even though only
    a small fraction changed.  We now preselect a deliberately broad candidate set
    in SQLite and normalize batches in worker processes; database mutations remain
    deterministic in the parent process.
    """
    stats = {
        "definitions_total": 0,
        "definitions_examined": 0,
        "candidates_selected": 0,
        "worker_processes": 1,
        "dal_headwords_normalized": 0,
        "dal_fallback_conflicts_removed": 0,
        "definitions_rewritten": 0,
        "definitions_removed": 0,
        "textual_aliases_converted": 0,
        "about_aliases_converted": 0,
        "alias_targets_linked": 0,
        "alias_fallback_definitions": 0,
        "old_equivalence_duplicates_removed": 0,
        "old_equivalence_retained": 0,
        "example_tails_removed": 0,
        "corpus_tails_removed": 0,
        "citations_removed": 0,
        "sense_references_removed": 0,
        "urls_removed": 0,
        "leading_metadata_removed": 0,
        "leading_context_rewritten": 0,
        "abbreviations_expanded": 0,
        "headword_prefixes_removed": 0,
        "empty_copulas_removed": 0,
        "bad_residues_removed": 0,
        "about_fragments_rewritten": 0,
        "dal_clusters_trimmed": 0,
        "dal_long_compacted": 0,
        "wikipedia_entity_noise_removed": 0,
        "wikipedia_extra_sentences_removed": 0,
        "wikipedia_history_tails_removed": 0,
        "wikipedia_named_cores_compacted": 0,
        "wikipedia_broken_tails_removed": 0,
        "wikipedia_broken_fragments_removed": 0,
        "wikipedia_list_residue_removed": 0,
        "trailing_descriptors_rewritten": 0,
        "duplicates_collapsed": 0,
        "wikipedia_rescue_candidates": 0,
    }
    conn.execute(
        "CREATE TABLE IF NOT EXISTS quality_rescue (lemma TEXT PRIMARY KEY) WITHOUT ROWID"
    )
    conn.execute("DELETE FROM quality_rescue")
    dal_normalized, dal_conflicts = _migrate_dal_headwords(
        conn,
        casefold_aliases=casefold_aliases,
        yo_aliases=yo_aliases,
        accent_aliases=accent_aliases,
    )
    stats["dal_headwords_normalized"] = dal_normalized
    stats["dal_fallback_conflicts_removed"] = dal_conflicts
    conn.commit()

    stats["definitions_total"] = int(conn.execute("SELECT COUNT(*) FROM senses").fetchone()[0])
    candidate_rows = _semantic_candidate_rows(conn)
    stats["candidates_selected"] = len(candidate_rows)

    cpu = max(1, os.cpu_count() or 1)
    workers = max(1, min(12, cpu - 1)) if len(candidate_rows) >= 50_000 else 1
    stats["worker_processes"] = workers
    batches = list(_batched_rows(candidate_rows, 2500))
    def apply_normalized_row(
        seq: int, lemma: str, definition: str, source: str,
        newdef: str, changes_tuple: tuple[str, ...],
    ) -> None:
        stats["definitions_examined"] += 1
        changes = set(changes_tuple)

        # Historical source placeholders such as
        # "Гора (аналогично русскому слову)" carry no extra meaning.
        if _is_old_equivalence_placeholder(lemma, newdef or definition, source):
            other = conn.execute(
                "SELECT 1 FROM senses WHERE lemma=? AND seq<>? LIMIT 1", (lemma, seq)
            ).fetchone()
            if other:
                conn.execute("DELETE FROM senses WHERE seq=?", (seq,))
                stats["definitions_removed"] += 1
                stats["old_equivalence_duplicates_removed"] += 1
                return
            conn.execute("INSERT OR IGNORE INTO quality_rescue(lemma) VALUES (?)", (lemma,))
            stats["old_equivalence_retained"] += 1

        # 4.7: short referential glosses such as "О коте" / "О Кубе" become
        # hidden aliases through the already-built morphology graph. No Russian
        # inflection is guessed here.
        if newdef and str(source).startswith("wiktionary"):
            about_targets = _quality_about_targets(conn, lemma, newdef)
            if about_targets:
                conn.execute("DELETE FROM senses WHERE seq=?", (seq,))
                for target in about_targets:
                    add_link(
                        conn, lemma, target,
                        casefold_aliases=casefold_aliases,
                        yo_aliases=yo_aliases,
                        accent_aliases=accent_aliases,
                    )
                stats["definitions_removed"] += 1
                stats["about_aliases_converted"] += 1
                stats["alias_targets_linked"] += len(about_targets)
                return

        # Explicit redirects are relation senses, not visible meanings.  Unlike
        # 4.6, remove just this sense even when the headword has other meanings.
        relation = _quality_alias_resolution(conn, lemma, newdef, source) if newdef else None
        if relation is not None:
            alias_targets, fallback_definition = relation
            if alias_targets:
                conn.execute("DELETE FROM senses WHERE seq=?", (seq,))
                for target in alias_targets:
                    add_link(
                        conn, lemma, target,
                        casefold_aliases=casefold_aliases,
                        yo_aliases=yo_aliases,
                        accent_aliases=accent_aliases,
                    )
                stats["definitions_removed"] += 1
                stats["textual_aliases_converted"] += 1
                stats["alias_targets_linked"] += len(alias_targets)
                return
            if fallback_definition and fallback_definition != newdef:
                # Alias fallback text is synthesized from a redirect's tail and
                # must pass the same narrow residue scrub as source definitions;
                # otherwise an unmatched quote/punctuation tail can be reintroduced
                # after the main normalizer has already cleaned the row.
                fallback_definition = clean_definition(fallback_definition)
                fallback_definition, fallback_changed = _strip_bad_residue(fallback_definition)
                if fallback_changed:
                    changes.add("bad_residue_removed")
                newdef = fallback_definition
                changes.add("alias_fallback_rewritten")
                stats["alias_fallback_definitions"] += 1

        if not newdef:
            conn.execute("DELETE FROM senses WHERE seq=?", (seq,))
            stats["definitions_removed"] += 1
            if str(source).startswith(("wiktionary:", "wikidata")):
                conn.execute("INSERT OR IGNORE INTO quality_rescue(lemma) VALUES (?)", (lemma,))
        elif newdef != definition:
            duplicate = conn.execute(
                "SELECT 1 FROM senses WHERE lemma=? AND definition=? AND seq<>? LIMIT 1",
                (lemma, newdef, seq),
            ).fetchone()
            if duplicate:
                conn.execute("DELETE FROM senses WHERE seq=?", (seq,))
                stats["definitions_removed"] += 1
                stats["duplicates_collapsed"] += 1
            else:
                conn.execute("UPDATE senses SET definition=? WHERE seq=?", (newdef, seq))
                stats["definitions_rewritten"] += 1

        if newdef and str(source).startswith(("wiktionary:", "wikidata")):
            weak_flags = set(definition_quality_flags(lemma, newdef, source))
            if weak_flags & {
                "very_short", "vague", "early_self_reference", "leading_parenthetical",
                "old_equivalence_placeholder", "url_residue", "broken_fragment",
                "about_fragment", "redirect_residue", "bad_residue",
            }:
                conn.execute("INSERT OR IGNORE INTO quality_rescue(lemma) VALUES (?)", (lemma,))

        for change, stat_key in (
            ("example_tail_removed", "example_tails_removed"),
            ("corpus_tail_removed", "corpus_tails_removed"),
            ("citation_removed", "citations_removed"),
            ("sense_reference_removed", "sense_references_removed"),
            ("url_removed", "urls_removed"),
            ("leading_metadata_removed", "leading_metadata_removed"),
            ("leading_context_rewritten", "leading_context_rewritten"),
            ("abbreviation_expanded", "abbreviations_expanded"),
            ("headword_prefix_removed", "headword_prefixes_removed"),
            ("empty_copula_removed", "empty_copulas_removed"),
            ("bad_residue_removed", "bad_residues_removed"),
            ("about_fragment_rewritten", "about_fragments_rewritten"),
            ("dal_cluster_trimmed", "dal_clusters_trimmed"),
            ("dal_long_compacted", "dal_long_compacted"),
            ("wikipedia_entity_noise_removed", "wikipedia_entity_noise_removed"),
            ("wikipedia_extra_sentences_removed", "wikipedia_extra_sentences_removed"),
            ("wikipedia_history_tail_removed", "wikipedia_history_tails_removed"),
            ("wikipedia_named_core_compacted", "wikipedia_named_cores_compacted"),
            ("wikipedia_broken_tail_removed", "wikipedia_broken_tails_removed"),
            ("wikipedia_broken_fragment_removed", "wikipedia_broken_fragments_removed"),
            ("wikipedia_list_residue_removed", "wikipedia_list_residue_removed"),
            ("trailing_descriptor_rewritten", "trailing_descriptors_rewritten"),
        ):
            if change in changes:
                stats[stat_key] += 1

    if workers > 1 and batches:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            normalized_batches = pool.map(_quality_normalize_batch, batches, chunksize=1)
            for normalized in normalized_batches:
                for row in normalized:
                    apply_normalized_row(*row)
                if stats["definitions_examined"]:
                    progress_render(
                        "Semantic cleanup", stats["definitions_examined"], len(candidate_rows), unit="candidates"
                    )
                if stats["definitions_examined"] % 50_000 < 2500:
                    conn.commit()
    else:
        for batch in batches:
            for row in _quality_normalize_batch(batch):
                apply_normalized_row(*row)
            progress_render("Semantic cleanup", stats["definitions_examined"], len(candidate_rows), unit="candidates")
            conn.commit()

    conn.commit()
    stats["wikipedia_rescue_candidates"] = conn.execute(
        "SELECT COUNT(*) FROM quality_rescue"
    ).fetchone()[0]
    if candidate_rows:
        progress_finish("Semantic cleanup", len(candidate_rows), len(candidate_rows), unit="candidates")
    return stats


def wikipedia_quality_rescue(
    prepared: Path | None,
    conn: sqlite3.Connection,
    *,
    casefold_aliases: bool = True,
    yo_aliases: bool = True,
    accent_aliases: bool = True,
) -> dict[str, int]:
    """Rescue definitions made missing/weak by the semantic cleanup.

    The normal Wikipedia import runs *before* semantic cleanup.  That means a
    Wiktionary placeholder can make a Wikipedia article look "already defined",
    after which the placeholder is correctly deleted by the cleaner and the word
    would otherwise end up definition-less.  This second pass is deliberately
    targeted: it scans titles from the compact prepared Wikipedia cache, but only
    decompresses/cleans leads whose exact title/base title is in quality_rescue.

    Missing lemmas are rescued only by an exact Wikipedia title.  Parenthesized
    base-title matches may upgrade an existing weak single sense, but never create
    a new potentially ambiguous base entry.  This keeps the pass conservative.
    """
    stats = {
        "targets": 0,
        "prepared_titles_scanned": 0,
        "matching_candidates": 0,
        "missing_definitions_rescued": 0,
        "weak_definitions_upgraded": 0,
        "rejected_candidates": 0,
    }
    try:
        targets = {row[0] for row in conn.execute("SELECT lemma FROM quality_rescue")}
    except sqlite3.DatabaseError:
        targets = set()
    stats["targets"] = len(targets)
    if not targets or prepared is None or not prepared.exists():
        conn.execute("DROP TABLE IF EXISTS quality_rescue")
        conn.commit()
        return stats

    pc = sqlite3.connect(prepared)
    matched_rows: list[tuple[str, str, bytes]] = []
    try:
        for title, categories, lead_z in pc.execute(
            "SELECT title,categories,lead_z FROM candidates ORDER BY title COLLATE BINARY"
        ):
            stats["prepared_titles_scanned"] += 1
            base_title = title.split(" (", 1)[0].strip() if " (" in title and title.endswith(")") else title
            if title in targets or base_title in targets:
                matched_rows.append((title, categories, lead_z))
    finally:
        pc.close()
    stats["matching_candidates"] = len(matched_rows)

    cpu = max(1, os.cpu_count() or 1)
    workers = max(1, min(8, cpu - 1)) if len(matched_rows) >= 2_000 else 1
    for batch in _parallel_cleaned_wiki_batches(iter(matched_rows), workers):
        for domain_ok, title, base_title, definition in batch:
            target = title if title in targets else (base_title if base_title in targets else None)
            if target is None or not domain_ok or not definition:
                stats["rejected_candidates"] += 1
                continue
            candidate, _changes = _quality_normalize_definition(title, definition, "ruwiki-quality")
            if not candidate:
                stats["rejected_candidates"] += 1
                continue

            existing = has_definition_for_key(conn, target)
            if existing:
                upgraded, _reason = maybe_upgrade_existing_definition(
                    conn, target, candidate, source="ruwiki-quality"
                )
                if upgraded:
                    stats["weak_definitions_upgraded"] += 1
                continue

            # A qualified title (e.g. "X (физика)") is not enough evidence to
            # recreate an unqualified X after its old placeholder was removed.
            if title != target:
                stats["rejected_candidates"] += 1
                continue
            score = definition_quality_score(target, candidate, "ruwiki-rescue")
            bad = set(definition_quality_flags(target, candidate, "ruwiki-rescue"))
            if score < 70 or bad & {"grammar_residue", "markup_residue", "placeholder_definition", "fragment"}:
                stats["rejected_candidates"] += 1
                continue
            if add_sense(conn, target, candidate, "ruwiki-rescue"):
                add_link(
                    conn, target, target,
                    casefold_aliases=casefold_aliases,
                    yo_aliases=yo_aliases,
                    accent_aliases=accent_aliases,
                )
                stats["missing_definitions_rescued"] += 1

    conn.execute("DROP TABLE IF EXISTS quality_rescue")
    conn.commit()
    return stats


def resolve_links(conn: sqlite3.Connection, max_rounds: int = 8) -> dict[str, int]:
    """Resolve alias -> alias -> lemma chains and discard targets with no definitions."""
    propagated = 0
    for _ in range(max_rounds):
        before = conn.total_changes
        conn.execute(
            """
            INSERT OR IGNORE INTO links(key, lemma)
            SELECT a.key, b.lemma
            FROM links AS a
            JOIN links AS b ON b.key = a.lemma
            WHERE NOT EXISTS (SELECT 1 FROM senses s WHERE s.lemma = a.lemma)
            """
        )
        conn.commit()
        added = conn.total_changes - before
        propagated += added
        if not added:
            break
    before = conn.total_changes
    conn.execute("DELETE FROM links WHERE NOT EXISTS (SELECT 1 FROM senses s WHERE s.lemma = links.lemma)")
    conn.commit()
    removed = conn.total_changes - before
    return {"propagated": propagated, "unresolved_removed": removed}


def _participle_candidates_for_verb(conn: sqlite3.Connection, verb: str) -> list[str]:
    verb = normalize_key(verb)
    candidates: list[str] = []
    for (key,) in conn.execute("SELECT key FROM links WHERE lemma=?", (verb,)):
        if not isinstance(key, str) or " " in key or len(key) > 70:
            continue
        low = key.casefold()
        if low == verb.casefold():
            continue
        if low.endswith(PARTICIPLE_ENDINGS):
            candidates.append(key)
    # Nominative masculine singular participles are normally the shortest forms
    # ending in -\u043d\u043d\u044b\u0439/-\u0442\u044b\u0439; prefer standard spellings with \u0451 when present.
    return sorted(set(candidates), key=lambda x: (len(x), 0 if "\u0451" in x.casefold() else 1, x.casefold()))


def _best_participle_for_verb(conn: sqlite3.Connection, verb: str) -> str | None:
    candidates = _participle_candidates_for_verb(conn, verb)
    return candidates[0] if candidates else None


def derive_passive_definition(
    conn: sqlite3.Connection,
    definition: str,
    participle_cache: dict[str, str | None] | None = None,
) -> str | None:
    """Conservatively turn a leading infinitive definition into a passive result.

    No Russian word is synthesized from spelling rules. Every replacement must
    already exist in the lookup graph as a form of the corresponding infinitive.
    This prevents invented participles and makes the transformation safe enough
    for an offline dictionary build.
    """
    clean = clean_definition(definition)
    if not clean:
        return None
    m = LEADING_INFINITIVE_CHAIN_RE.match(clean)
    if not m:
        return None
    prefix = m.group("verbs")
    rest = m.group("rest")
    parts = re.split(r"(\s+(?:\u0438|\u0438\u043b\u0438)\s+)", prefix, flags=re.IGNORECASE)
    converted: list[str] = []
    changed = 0
    for part in parts:
        if re.fullmatch(r"\s+(?:\u0438|\u0438\u043b\u0438)\s+", part, flags=re.IGNORECASE):
            converted.append(part)
            continue
        verb = part.strip()
        verb_key = verb.casefold()
        if participle_cache is not None and verb_key in participle_cache:
            participle = participle_cache[verb_key]
        else:
            participle = _best_participle_for_verb(conn, verb_key)
            if participle_cache is not None:
                participle_cache[verb_key] = participle
        if not participle:
            return None
        if verb[:1].isupper():
            participle = participle[:1].upper() + participle[1:]
        converted.append(participle)
        changed += 1
    if not changed:
        return None
    # Direct-object placeholders belong to the infinitive wording and become
    # ungrammatical after the passive transformation.
    rest = re.sub(
        r"^\s+(?:(?:\u0447\u0442\u043e|\u043a\u043e\u0433\u043e)-(?:\u043b\u0438\u0431\u043e|\u043d\u0438\u0431\u0443\u0434\u044c)|\u0447\u0442\u043e-\u0442\u043e|\u043a\u043e\u0433\u043e-\u0442\u043e)\b",
        "",
        rest,
        flags=re.IGNORECASE,
    )
    result = "".join(converted) + rest
    result = WS_RE.sub(" ", result).strip()
    return clean_definition(result) or None


def materialize_form_overrides(conn: sqlite3.Connection) -> dict[str, int]:
    """Create display-only meanings for safe passive-participle redirects."""
    considered = generated = skipped_direct = skipped_ambiguous = 0
    participle_cache: dict[str, str | None] = {}
    target_definition_cache: dict[str, list[str]] = {}
    for key, target in conn.execute(
        "SELECT key, target FROM form_hints WHERE kind='passive_participle' ORDER BY key, target"
    ):
        considered += 1
        if has_direct_sense(conn, key):
            skipped_direct += 1
            continue
        target_defs = target_definition_cache.get(target)
        if target_defs is None:
            target_defs = [r[0] for r in conn.execute(
                "SELECT definition FROM senses WHERE lemma=? ORDER BY seq", (target,)
            )]
            target_definition_cache[target] = target_defs
        if len(target_defs) != 1:
            skipped_ambiguous += 1
            continue
        derived = derive_passive_definition(conn, target_defs[0], participle_cache)
        if not derived:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO lookup_overrides(key, definition, source) VALUES (?, ?, ?)",
            (key, derived, "derived-passive"),
        )
        generated += 1
    conn.commit()
    return {
        "hints_considered": considered,
        "display_overrides_generated": generated,
        "skipped_direct_lexical_sense": skipped_direct,
        "skipped_ambiguous_target": skipped_ambiguous,
    }


def _quality_examples_bucket() -> dict[str, list[dict[str, str]]]:
    return {}


def write_quality_report(
    conn: sqlite3.Connection,
    output_dir: Path,
    *,
    max_examples: int = 80,
    review_limit: int = 5000,
    onomastic_limit: int = 2000,
) -> dict[str, object]:
    """Audit canonical senses and write actionable + onomastic QA views.

    4.5's 5,000-row review was almost completely occupied by perfectly valid
    entries like "Мужское имя", "Русская фамилия" and "Река в России".  4.6
    keeps those meanings, scores them as concise onomastic information, and moves
    them to a separate review file so the main queue exposes real cleanup work.
    """
    counts: dict[str, int] = {}
    informational: dict[str, int] = {}
    examples: dict[str, list[dict[str, str]]] = {}
    source_counts: dict[str, int] = {}
    total = 0
    score_sum = 0
    score_buckets = {"0-49": 0, "50-69": 0, "70-84": 0, "85-100": 0}
    review_heap: list[tuple[int, int, str, str, str, str]] = []
    onomastic_rows: list[tuple[int, str, str, str]] = []
    concise_rows: list[tuple[int, str, str, str]] = []
    expected_defs = conn.execute("SELECT COUNT(*) FROM senses").fetchone()[0]

    for lemma, definition, source in conn.execute(
        "SELECT lemma, definition, source FROM senses ORDER BY lemma COLLATE BINARY, seq"
    ):
        total += 1
        if total % 25_000 == 0:
            progress_render("Quality audit", total, expected_defs, unit="definitions")
        source_counts[source] = source_counts.get(source, 0) + 1
        compact = _compact_quality_text(definition)
        flags_for_definition = definition_quality_flags(lemma, compact, source)
        score = definition_quality_score(
            lemma, compact, source, flags_for_definition, _normalized_text=compact
        )
        score_sum += score
        if score < 50:
            score_buckets["0-49"] += 1
        elif score < 70:
            score_buckets["50-69"] += 1
        elif score < 85:
            score_buckets["70-84"] += 1
        else:
            score_buckets["85-100"] += 1

        informational_flags = {"onomastic_stub", "concise_gloss"}
        actionable_flags = [f for f in flags_for_definition if f not in informational_flags]
        if "onomastic_stub" in flags_for_definition:
            informational["onomastic_stub"] = informational.get("onomastic_stub", 0) + 1
            if len(onomastic_rows) < onomastic_limit:
                onomastic_rows.append((score, lemma, source, compact))
        if "concise_gloss" in flags_for_definition:
            informational["concise_gloss"] = informational.get("concise_gloss", 0) + 1
            if len(concise_rows) < onomastic_limit:
                concise_rows.append((score, lemma, source, compact))

        for flag in actionable_flags:
            counts[flag] = counts.get(flag, 0) + 1
            bucket = examples.setdefault(flag, [])
            if len(bucket) < max_examples:
                bucket.append({"word": lemma, "definition": compact, "source": source, "score": str(score)})

        # Do not let concise names/toponyms drown the actionable review. They are
        # visible separately in QUALITY_ONOMASTICS.tsv.
        if review_limit > 0 and not informational_flags.intersection(flags_for_definition) and (actionable_flags or score < 60):
            entry = (-score, total, lemma, source, ",".join(actionable_flags), compact)
            if len(review_heap) < review_limit:
                heapq.heappush(review_heap, entry)
            elif entry[0] > review_heap[0][0]:
                heapq.heapreplace(review_heap, entry)

    if total:
        progress_finish("Quality audit", total, expected_defs or total, unit="definitions")
    overrides = conn.execute("SELECT COUNT(*) FROM lookup_overrides").fetchone()[0]
    report: dict[str, object] = {
        "builder_version": BUILDER_VERSION,
        "definitions_audited": total,
        "average_quality_score": round(score_sum / total, 2) if total else 0,
        "score_buckets": score_buckets,
        "warning_counts": dict(sorted(counts.items())),
        "informational_counts": dict(sorted(informational.items())),
        "display_overrides": overrides,
        "source_definition_counts": dict(sorted(source_counts.items())),
        "review_rows": len(review_heap),
        "review_file": "QUALITY_REVIEW.tsv",
        "onomastic_review_rows": len(onomastic_rows),
        "onomastic_review_file": "QUALITY_ONOMASTICS.tsv",
        "concise_review_rows": len(concise_rows),
        "concise_review_file": "QUALITY_CONCISE.tsv",
        "examples": examples,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "QUALITY_REPORT.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    warning_labels = {
        "encyclopedic_date": "энциклопедические даты/справки",
        "very_short": "слишком короткие определения",
        "early_self_reference": "самоссылка в начале определения",
        "leading_parenthetical": "начальная скобочная помета",
        "very_long": "слишком длинные определения",
        "grammar_residue": "остатки грамматических описаний",
        "markup_residue": "остатки wiki/HTML-разметки",
        "vague": "слишком расплывчатые определения",
        "example_residue": "остатки примеров/корпусных цитат",
        "placeholder_definition": "служебные заглушки вместо значения",
        "fragment": "оборванные фрагменты определения",
        "old_equivalence_placeholder": "заглушки «аналогично русскому слову»",
        "url_residue": "URL вместо словарного значения",
        "broken_fragment": "сломанные/оборванные Wikipedia-фрагменты",
        "redirect_residue": "неразрешённые текстовые перенаправления",
        "about_fragment": "описание о слове вместо значения",
        "bad_residue": "пустые/оборванные пунктуационные хвосты",
    }
    lines = [
        f"RU Max Clean {BUILDER_VERSION} — отчёт контроля качества",
        "=" * 58,
        f"Проверено определений: {total:,}".replace(",", " "),
        f"Средний эвристический балл: {report['average_quality_score']} / 100",
        f"Естественных определений словоформ: {overrides:,}".replace(",", " "),
        f"Кандидатов на ручную проверку: {len(review_heap):,}".replace(",", " "),
        "",
        "Предупреждения:",
    ]
    if counts:
        for name, count in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
            label = warning_labels.get(name, name)
            lines.append(f"  {label}: {count:,}".replace(",", " "))
    else:
        lines.append("  нет")
    if informational:
        lines.extend(["", "Информационные классы (не считаются ошибками):"])
        if informational.get("onomastic_stub"):
            lines.append(
                f"  краткие имена/фамилии/топонимы: {informational['onomastic_stub']:,}".replace(",", " ")
            )
        if informational.get("concise_gloss"):
            lines.append(
                f"  корректные краткие значения/синонимы: {informational['concise_gloss']:,}".replace(",", " ")
            )
    lines.extend(["", "Распределение по качеству:"])
    for name, count in score_buckets.items():
        lines.append(f"  {name}: {count:,}".replace(",", " "))
    lines.extend([
        "",
        "QUALITY_REVIEW.tsv содержит только реальные кандидаты на улучшение.",
        "QUALITY_ONOMASTICS.tsv отдельно содержит краткие справочные имена/топонимы.",
        "QUALITY_CONCISE.tsv отдельно содержит корректные короткие значения/синонимы.",
        "Предупреждения — подсказки для контроля, а не правила автоматического удаления.",
    ])
    (output_dir / "QUALITY_REPORT.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    review_rows = sorted(review_heap, key=lambda row: (-row[0], row[2].casefold(), row[1]))
    with (output_dir / "QUALITY_REVIEW.tsv").open("w", encoding="utf-8", newline="\n") as f:
        f.write("score\twarnings\tword\tsource\tdefinition\n")
        for neg_score, _ordinal, lemma, source, flags_text, definition in review_rows:
            clean_fields = [
                str(-neg_score), flags_text,
                _compact_quality_text(lemma).replace("\t", " "),
                _compact_quality_text(source).replace("\t", " "),
                _compact_quality_text(definition).replace("\t", " "),
            ]
            f.write("\t".join(clean_fields) + "\n")

    with (output_dir / "QUALITY_ONOMASTICS.tsv").open("w", encoding="utf-8", newline="\n") as f:
        f.write("score\tword\tsource\tdefinition\n")
        for score, lemma, source, definition in onomastic_rows:
            f.write("\t".join([
                str(score), _compact_quality_text(lemma).replace("\t", " "),
                _compact_quality_text(source).replace("\t", " "),
                _compact_quality_text(definition).replace("\t", " "),
            ]) + "\n")

    with (output_dir / "QUALITY_CONCISE.tsv").open("w", encoding="utf-8", newline="\n") as f:
        f.write("score\tword\tsource\tdefinition\n")
        for score, lemma, source, definition in concise_rows:
            f.write("\t".join([
                str(score), _compact_quality_text(lemma).replace("\t", " "),
                _compact_quality_text(source).replace("\t", " "),
                _compact_quality_text(definition).replace("\t", " "),
            ]) + "\n")
    return report


def format_article(definitions: Iterable[str]) -> str:
    clean: list[str] = []
    seen: set[str] = set()
    for definition in definitions:
        d = clean_definition(definition)
        if not d:
            continue
        fingerprint = WS_RE.sub(" ", d.casefold()).strip(" .;,:—-")
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        clean.append(d)
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0]
    return "\n".join(f"{i}. {d}" for i, d in enumerate(clean, 1))


def _write_idx_entry(out, key: str, offset: int, size: int) -> bool:
    key_bytes = key.encode("utf-8")
    if len(key_bytes) > 255:
        # sdcv/KOReader has historically had trouble with very long index words.
        return False
    if offset > 0xFFFFFFFF or size > 0xFFFFFFFF:
        raise RuntimeError("StarDict 2.4.2 32-bit offset/size limit exceeded")
    out.write(key_bytes + b"\x00" + struct.pack(">II", offset, size))
    return True


def _split_formatted_article(article: str) -> list[str]:
    """Reverse format_article for canonical bodies used in ambiguous lookups."""
    lines = [line.strip() for line in article.splitlines() if line.strip()]
    if len(lines) > 1 and all(re.match(r"^\d+\.\s+", line) for line in lines):
        return [re.sub(r"^\d+\.\s+", "", line, count=1) for line in lines]
    return [article] if article.strip() else []


def build_stardict(
    conn: sqlite3.Connection,
    output_dir: Path,
    *,
    bookname: str = "RU Max Clean",
) -> dict[str, object]:
    """Write StarDict with an in-memory offset map and a batched index writer.

    4.4 spent most of a cached rebuild inside export.  The old implementation
    inserted ~500k article offsets back into SQLite, joined them against 6.5M links,
    globally sorted a UNION, then executed up to ~100k extra SQL queries for
    ambiguous forms.  4.5 keeps the compact offset map in RAM (well below 100 MiB
    on the real database), streams links in their existing PK order, merges the tiny
    override stream in Python, and writes .idx in multi-megabyte chunks.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    base = output_dir / "ru-max-clean"
    dict_path = base.with_suffix(".dict")
    idx_path = base.with_suffix(".idx")
    ifo_path = base.with_suffix(".ifo")

    expected_articles = conn.execute("SELECT COUNT(DISTINCT lemma) FROM senses").fetchone()[0]
    expected_keys = conn.execute("SELECT COUNT(DISTINCT key) FROM links").fetchone()[0]
    canonical_articles = 0
    article_offsets: dict[str, tuple[int, int]] = {}
    article_texts: dict[str, str] = {}

    with dict_path.open("w+b", buffering=8 * 1024 * 1024) as dict_out:
        current_lemma: str | None = None
        current_defs: list[str] = []

        def flush_lemma() -> None:
            nonlocal canonical_articles, current_lemma, current_defs
            if current_lemma is None:
                return
            article = format_article(current_defs)
            if not article:
                return
            body = article.encode("utf-8")
            off = dict_out.tell()
            dict_out.write(body)
            article_offsets[current_lemma] = (off, len(body))
            article_texts[current_lemma] = article
            canonical_articles += 1
            if canonical_articles % 25_000 == 0:
                progress_render("StarDict articles", canonical_articles, expected_articles, unit="articles")

        for lemma, definition in conn.execute(
            "SELECT lemma, definition FROM senses ORDER BY lemma COLLATE BINARY, seq"
        ):
            if lemma != current_lemma:
                flush_lemma()
                current_lemma = lemma
                current_defs = [definition]
            else:
                current_defs.append(definition)
        flush_lemma()
        if canonical_articles:
            progress_finish("StarDict articles", canonical_articles, expected_articles or canonical_articles, unit="articles")

        # Display-only meanings are few (~13k) and often share identical bodies.
        override_body_cache: dict[str, tuple[int, int]] = {}
        override_offsets: dict[str, tuple[int, int]] = {}
        override_count = 0
        for key, definition in conn.execute(
            "SELECT key, definition FROM lookup_overrides ORDER BY key COLLATE BINARY"
        ):
            article = clean_definition(definition)
            if not article:
                continue
            cached = override_body_cache.get(article)
            if cached is None:
                body = article.encode("utf-8")
                off = dict_out.tell()
                dict_out.write(body)
                cached = (off, len(body))
                override_body_cache[article] = cached
            override_offsets[key] = cached
            override_count += 1

        dict_out.flush()

        wordcount = 0
        reused_aliases = 0
        ambiguous_keys = 0
        ambiguous_cache: dict[tuple[str, ...], tuple[int, int]] = {}

        def link_groups():
            cur_key: str | None = None
            targets: list[tuple[str, int, int]] = []
            for key, lemma in conn.execute(
                "SELECT key, lemma FROM links ORDER BY key COLLATE BINARY, lemma COLLATE BINARY"
            ):
                pos = article_offsets.get(lemma)
                if pos is None:
                    continue
                if key != cur_key:
                    if cur_key is not None and targets:
                        yield cur_key, targets
                    cur_key = key
                    targets = [(lemma, pos[0], pos[1])]
                else:
                    targets.append((lemma, pos[0], pos[1]))
            if cur_key is not None and targets:
                yield cur_key, targets

        override_items = iter(sorted(override_offsets.items(), key=lambda kv: kv[0]))
        link_items = iter(link_groups())
        try:
            current_override = next(override_items)
        except StopIteration:
            current_override = None
        try:
            current_link = next(link_items)
        except StopIteration:
            current_link = None

        with idx_path.open("wb", buffering=8 * 1024 * 1024) as idx_out:
            idx_buffer = bytearray()
            IDX_FLUSH = 8 * 1024 * 1024

            def write_idx(key: str, off: int, size: int) -> bool:
                nonlocal idx_buffer
                key_bytes = key.encode("utf-8")
                if len(key_bytes) > 255:
                    return False
                if off > 0xFFFFFFFF or size > 0xFFFFFFFF:
                    raise RuntimeError("StarDict 2.4.2 32-bit offset/size limit exceeded")
                idx_buffer.extend(key_bytes)
                idx_buffer.append(0)
                idx_buffer.extend(struct.pack(">II", off, size))
                if len(idx_buffer) >= IDX_FLUSH:
                    idx_out.write(idx_buffer)
                    idx_buffer.clear()
                return True

            def emit_single(key: str, pos: tuple[int, int]) -> None:
                nonlocal wordcount, reused_aliases
                if write_idx(key, pos[0], pos[1]):
                    reused_aliases += 1
                    wordcount += 1

            def emit_link_group(key: str, targets: list[tuple[str, int, int]]) -> None:
                nonlocal wordcount, reused_aliases, ambiguous_keys
                uniq: list[tuple[str, int, int]] = []
                seen_lemmas: set[str] = set()
                for item in targets:
                    if item[0] not in seen_lemmas:
                        seen_lemmas.add(item[0])
                        uniq.append(item)
                if len(uniq) == 1:
                    if write_idx(key, uniq[0][1], uniq[0][2]):
                        reused_aliases += 1
                        wordcount += 1
                    return
                ambiguous_keys += 1
                sig = tuple(item[0] for item in uniq)
                cached = ambiguous_cache.get(sig)
                if cached is None:
                    defs: list[str] = []
                    for _lemma, _off, _size in uniq:
                        article_text = article_texts.get(_lemma, "")
                        defs.extend(_split_formatted_article(article_text))
                    article = format_article(defs)
                    if not article:
                        return
                    body = article.encode("utf-8")
                    dict_out.seek(0, os.SEEK_END)
                    off = dict_out.tell()
                    dict_out.write(body)
                    cached = (off, len(body))
                    ambiguous_cache[sig] = cached
                if write_idx(key, cached[0], cached[1]):
                    wordcount += 1

            # Merge two already-sorted streams. An override suppresses the ordinary
            # link group for the same key, matching the old UNION/NOT EXISTS logic.
            while current_override is not None or current_link is not None:
                if current_link is None:
                    okey, opos = current_override
                    emit_single(okey, opos)
                    try:
                        current_override = next(override_items)
                    except StopIteration:
                        current_override = None
                elif current_override is None:
                    lkey, ltargets = current_link
                    emit_link_group(lkey, ltargets)
                    try:
                        current_link = next(link_items)
                    except StopIteration:
                        current_link = None
                else:
                    okey, opos = current_override
                    lkey, ltargets = current_link
                    if okey < lkey:
                        emit_single(okey, opos)
                        try:
                            current_override = next(override_items)
                        except StopIteration:
                            current_override = None
                    elif okey == lkey:
                        emit_single(okey, opos)
                        try:
                            current_override = next(override_items)
                        except StopIteration:
                            current_override = None
                        try:
                            current_link = next(link_items)
                        except StopIteration:
                            current_link = None
                    else:
                        emit_link_group(lkey, ltargets)
                        try:
                            current_link = next(link_items)
                        except StopIteration:
                            current_link = None
                if wordcount and wordcount % 50_000 == 0:
                    progress_render("StarDict index", wordcount, expected_keys, unit="keys")
            if idx_buffer:
                idx_out.write(idx_buffer)
                idx_buffer.clear()
            if wordcount:
                progress_finish("StarDict index", wordcount, expected_keys or wordcount, unit="keys")

    idx_size = idx_path.stat().st_size
    dict_size = dict_path.stat().st_size
    date = dt.datetime.now(dt.timezone.utc).strftime("%Y.%m.%d")
    ifo_text = (
        "StarDict's dict ifo file\n"
        "version=2.4.2\n"
        f"wordcount={wordcount}\n"
        f"idxfilesize={idx_size}\n"
        f"bookname={bookname}\n"
        "sametypesequence=m\n"
        "description=Definition-only Russian MAX dictionary with semantic cleanup, form-aware display, historical and specialist fallback layers.\n"
        f"date={date}\n"
    )
    ifo_path.write_text(ifo_text, encoding="utf-8", newline="\n")
    return {
        "wordcount": wordcount,
        "canonical_articles": canonical_articles,
        "single_target_keys": reused_aliases,
        "ambiguous_keys": ambiguous_keys,
        "ambiguous_article_sets": len(ambiguous_cache),
        "display_overrides": override_count,
        "unique_override_bodies": len(override_body_cache),
        "dict_bytes": dict_size,
        "idx_bytes": idx_size,
        "files": [str(ifo_path), str(idx_path), str(dict_path)],
    }


def discover_extras(extra_dir: Path | None) -> tuple[list[Path], list[Path]]:
    if not extra_dir or not extra_dir.exists():
        return [], []
    return sorted(extra_dir.glob("*.tsv")), sorted(list(extra_dir.glob("*.jsonl")) + list(extra_dir.glob("*.jsonl.gz")))


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Build RU Max Clean StarDict for KOReader (definitions only).")
    p.add_argument("--kaikki", help="Path to ruwiktionary raw-wiktextract-data.jsonl.gz")
    p.add_argument("--download-kaikki", action="store_true", help="Check/download current Kaikki Russian Wiktionary extract")
    p.add_argument("--dal", help="Path to Dal StarDict archive/directory")
    p.add_argument("--download-dal", action="store_true", help="Check/download Dal StarDict fallback layer")
    p.add_argument("--dal-merge", action="store_true", help="Also append Dal meanings to words already defined by better-priority sources")
    p.add_argument("--wikidata-lexemes", help="Path to Wikidata latest-lexemes.json.bz2")
    p.add_argument("--download-wikidata-lexemes", action="store_true", help="Check/download Wikidata Lexeme dump (Russian gloss/form expansion)")
    p.add_argument("--wikidata-merge", action="store_true", help="Append Wikidata glosses even when Wiktionary already defines the lemma")
    p.add_argument("--wikipedia", help="Path to Russian Wikipedia pages-articles XML.bz2")
    p.add_argument("--download-wikipedia", action="store_true", help="Check/download Russian Wikipedia (~6 GB) for specialist fallback definitions")
    p.add_argument("--wikipedia-merge", action="store_true", help="Append Wikipedia leads even for already defined terms (not recommended)")
    p.add_argument("--wikipedia-quality-upgrade", action="store_true", help="Conservatively replace only weak single Wiktionary/Wikidata definitions with clearly better Wikipedia leads")
    p.add_argument("--no-quality-report", action="store_true", help="Skip QUALITY_REPORT.json/txt generation")
    p.add_argument("--legacy-stardict", action="append", default=[], help="Import another StarDict archive as fallback (m/h text dictionaries)")
    p.add_argument("--extra-tsv", action="append", default=[], help="word<TAB>definition<TAB>alias1|alias2")
    p.add_argument("--extra-jsonl", action="append", default=[], help='JSONL: {"word":...,"definitions":[...],"aliases":[...]}')
    p.add_argument("--extra-dir", default="extras", help="Auto-import *.tsv and *.jsonl[.gz] from this directory")
    p.add_argument("--output-dir", default="RU-Max-Clean")
    p.add_argument("--cache-dir", default="sources")
    p.add_argument("--db", help="Temporary SQLite path")
    p.add_argument("--langs", default=",".join(DEFAULT_LANGS), help="Wiktextract lang codes")
    p.add_argument("--no-forms", action="store_true")
    p.add_argument("--no-casefold-aliases", action="store_true")
    p.add_argument("--no-yo-aliases", action="store_true")
    p.add_argument("--no-accent-aliases", action="store_true")
    p.add_argument("--offline", action="store_true", help="Do not contact source servers; use cached files only")
    p.add_argument("--force-refresh", action="store_true", help="Redownload enabled online sources even when metadata says unchanged")
    p.add_argument("--keep-db", action="store_true")
    p.add_argument("--no-stage-cache", action="store_true", help="Disable parsed pipeline-stage caches")
    p.add_argument("--rebuild-stage-cache", action="store_true", help="Ignore existing parsed stage caches and recreate them")
    p.add_argument("--no-artifact-cache", action="store_true", help="Disable final StarDict artifact cache")
    p.add_argument(
        "--restore-artifact", action="store_true",
        help="Cache-only mode: restore the latest complete StarDict artifact without source dumps",
    )
    p.add_argument("--max-records", type=int, default=0, help="Testing only: limit Kaikki/Wikidata records")
    p.add_argument("--max-wikipedia-pages", type=int, default=0, help="Testing only: limit Wikipedia pages")
    return p.parse_args(argv)


def _source_info(cache_dir: Path, out_dir: Path, result: dict[str, object]) -> None:
    """Copy build/source metadata next to StarDict without polluting popup articles."""
    manifest_path = cache_dir / "source_manifest.json"
    try:
        sources = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        sources = {}
    # A user may reuse a source cache created by a much older builder.  Keep
    # current metadata useful, but never copy retired source labels/URLs into
    # the new BUILD_INFO.json artifact.
    if isinstance(sources, dict):
        retired_marker = "open" + "corpora"
        sources = {
            key: value for key, value in sources.items()
            if retired_marker not in json.dumps({key: value}, ensure_ascii=False).casefold()
        }
    info = {
        "name": "RU Max Clean",
        "builder_version": BUILDER_VERSION,
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "star_dict": result,
        "sources": sources,
    }
    (out_dir / "BUILD_INFO.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    notice = (
        "RU Max Clean data sources\n"
        "=========================\n\n"
        "Russian Wiktionary/Kaikki: Wikimedia/Wiktionary content; see project licensing.\n"
        "Wikidata Lexemes: structured lexicographical data under CC0.\n"
        "Russian Wikipedia fallback leads: text under CC BY-SA; used only when enabled and no higher-priority definition exists.\n"
        "V. I. Dal StarDict layer: historical fallback source.\n"
        "Source/version metadata for this particular build is stored in BUILD_INFO.json.\n"
        "No source labels are inserted into dictionary popup definitions.\n"
    )
    (out_dir / "SOURCES.txt").write_text(notice, encoding="utf-8")


def _source_stats_report(source_stats: dict[str, object]) -> None:
    stats = source_stats.get("wiktionary")
    if isinstance(stats, dict): report.source_wiktionary(stats)
    stats = source_stats.get("wikidata_lexemes")
    if isinstance(stats, dict): report.source_wikidata(stats)
    stats = source_stats.get("dal")
    if isinstance(stats, dict): report.source_dal(stats)
    stats = source_stats.get("wikipedia")
    if isinstance(stats, dict): report.source_wikipedia(stats)


def _stage_source_payload(path: Path | None) -> object:
    return file_fingerprint(path)


def _stage_cache_allowed(args) -> bool:
    # Test/partial builds intentionally bypass persistent stage snapshots.
    return not (args.no_stage_cache or args.max_records or args.max_wikipedia_pages)


def _restore_stage(cache: StageCache, name: str, sig: str, db_path: Path) -> tuple[sqlite3.Connection, dict[str, object]] | None:
    if not cache.valid(name, sig):
        return None
    t0 = time.perf_counter()
    meta = cache.restore(name, db_path)
    elapsed = time.perf_counter() - t0
    print(f"[КЭШ ЭТАПА] {name}: восстановлен за {elapsed:.1f} с")
    conn = open_existing_db(db_path)
    stats = meta.get("stats") if isinstance(meta, dict) else {}
    return conn, stats if isinstance(stats, dict) else {}


def _save_stage(cache: StageCache, name: str, sig: str, db_path: Path, conn: sqlite3.Connection, stats: dict[str, object]) -> None:
    conn.commit()
    t0 = time.perf_counter()
    meta = cache.save(name, db_path, sig, stats=stats, source_conn=conn)
    elapsed = time.perf_counter() - t0
    size = int(meta.get("db_size", 0)) if isinstance(meta, dict) else 0
    print(f"[КЭШ ЭТАПА] {name}: сохранён ({size / (1024**2):.1f} МиБ) за {elapsed:.1f} с")


def main(argv=None) -> int:
    args = parse_args(argv)
    timings = BuildTimings()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    load_progress_totals(cache_dir)
    cache = SourceCache(cache_dir, offline=args.offline, force_refresh=args.force_refresh)

    if args.restore_artifact:
        artifact_cache = ArtifactCache(cache_dir)
        restored = artifact_cache.restore_latest("stardict-max", out_dir)
        if restored is None:
            raise SystemExit(
                "Готовый StarDict-кэш не найден или повреждён. "
                "Сначала выполните пункт 1/3 для загрузки исходников."
            )
        nested = restored.get("stardict") if isinstance(restored, dict) else None
        result = nested if isinstance(nested, dict) else {}
        _source_info(cache_dir, out_dir, result)
        print(f"[КЭШ STARDICT] stardict-max: восстановлен без исходных дампов ({artifact_cache.describe('stardict-max')})")
        return 0

    # Online flags mean "check/update the rolling object, then use the cached copy".
    kaikkki_t0 = time.perf_counter()
    kaikki = Path(args.kaikki) if args.kaikki else None
    if args.download_kaikki:
        kaikki = cache.ensure(
            "Kaikki / Russian Wiktionary", KAIKKI_URL,
            kaikki or (cache_dir / "raw-wiktextract-data.jsonl.gz"), required=True,
        )
    if kaikki is None or not kaikki.exists():
        raise SystemExit("Kaikki input missing. Use --kaikki FILE or --download-kaikki.")
    timings.mark("Проверка/получение Wiktionary", time.perf_counter() - kaikkki_t0)

    wikidata_lexemes = Path(args.wikidata_lexemes) if args.wikidata_lexemes else None
    if args.download_wikidata_lexemes:
        t0 = time.perf_counter()
        wikidata_lexemes = cache.ensure(
            "Wikidata Lexemes", WIKIDATA_LEXEMES_URL,
            wikidata_lexemes or (cache_dir / "latest-lexemes.json.bz2"), required=False,
        )
        timings.mark("Проверка/получение Wikidata", time.perf_counter() - t0)
        if wikidata_lexemes is None:
            log("WARNING: Continuing without Wikidata Lexemes expansion.")

    dal = Path(args.dal) if args.dal else None
    if args.download_dal:
        t0 = time.perf_counter()
        dal = cache.ensure(
            "Dal historical dictionary", DAL_URLS,
            dal or (cache_dir / "stardict-dal-ru-2.4.2.tar.bz2"), required=False,
        )
        timings.mark("Проверка/получение Даля", time.perf_counter() - t0)
        if dal is None:
            log("WARNING: Continuing without the Dal historical fallback layer.")

    wikipedia = Path(args.wikipedia) if args.wikipedia else None
    if args.download_wikipedia:
        t0 = time.perf_counter()
        wikipedia = cache.ensure(
            "Russian Wikipedia terminology", RUWIKI_URL,
            wikipedia or (cache_dir / "ruwiki-latest-pages-articles.xml.bz2"), required=False,
        )
        timings.mark("Проверка/получение Wikipedia", time.perf_counter() - t0)
        if wikipedia is None:
            log("WARNING: Continuing without Wikipedia specialist fallback definitions.")

    db_path = Path(args.db) if args.db else out_dir / "ru-max-clean-build.sqlite3"
    if db_path.exists():
        db_path.unlink()

    flags = dict(
        casefold_aliases=not args.no_casefold_aliases,
        yo_aliases=not args.no_yo_aliases,
        accent_aliases=not args.no_accent_aliases,
    )
    langs = {x.strip() for x in args.langs.split(",") if x.strip()}
    extra_tsv = [Path(x) for x in args.extra_tsv]
    extra_jsonl = [Path(x) for x in args.extra_jsonl]
    auto_tsv, auto_jsonl = discover_extras(Path(args.extra_dir) if args.extra_dir else None)
    all_extra_files = [p for p in (extra_tsv + auto_tsv + extra_jsonl + auto_jsonl) if p.exists()]
    legacy_paths = [Path(x) for x in args.legacy_stardict if Path(x).exists()]

    print()
    print("Целевые языки:", ", ".join(sorted(langs)))
    turbo_stats = accelerator_info()
    report.turbo(turbo_stats)
    build_stats: dict[str, object] = {
        "builder_version": BUILDER_VERSION,
        "languages": sorted(langs),
        "turbo": turbo_stats,
        "sources": {},
    }
    source_stats = build_stats["sources"]
    assert isinstance(source_stats, dict)

    # ------------------------------------------------------------------
    # Persistent parsed stage caches. The expensive source parsing is now a
    # pipeline artifact, not something repeated every time the reporting/export
    # code changes. Stage rule versions are intentionally independent from the
    # top-level builder version.
    # ------------------------------------------------------------------
    stage_cache = StageCache(cache_dir)
    stage_enabled = _stage_cache_allowed(args)
    stage_flags = {
        "langs": sorted(langs), "forms": not args.no_forms,
        "casefold": flags["casefold_aliases"], "yo": flags["yo_aliases"],
        "accent": flags["accent_aliases"], "wikidata_merge": args.wikidata_merge,
        "dal_merge": args.dal_merge,
    }
    lexical_payload = {
        "rules": LEXICAL_STAGE_RULES,
        "flags": stage_flags,
        "kaikki": _stage_source_payload(kaikki),
        "wikidata": _stage_source_payload(wikidata_lexemes),
        "dal": _stage_source_payload(dal),
        "legacy": [_stage_source_payload(p) for p in legacy_paths],
        "extras": files_fingerprint(all_extra_files),
    }
    lexical_sig = stage_signature(lexical_payload)
    source_stage_sig = lexical_sig
    max_sig: str | None = None
    if wikipedia and wikipedia.exists():
        max_sig = stage_signature({
            "rules": WIKIPEDIA_STAGE_RULES,
            "lexical": lexical_sig,
            "wikipedia": _stage_source_payload(wikipedia),
            "merge": args.wikipedia_merge,
            "quality_upgrade": args.wikipedia_quality_upgrade,
        })
        source_stage_sig = max_sig
    profile_name = "max" if max_sig else "lexical"
    quality_sig = stage_signature({
        "rules": QUALITY_STAGE_RULES,
        "input": source_stage_sig,
        # Source/max caches remain reusable, but quality and downstream stages
        # must never silently reuse an artifact built by different code.
        "builder_code": _builder_code_sha256(),
    })
    resolved_sig = stage_signature({"rules": RESOLVE_STAGE_RULES, "input": quality_sig})
    form_sig = stage_signature({"rules": FORM_STAGE_RULES, "input": resolved_sig})
    export_sig = stage_signature({
        "rules": EXPORT_STAGE_RULES, "input": form_sig, "bookname": "RU Max Clean",
        "quality_audit_rules": QUALITY_AUDIT_RULES,
        "quality_report": not args.no_quality_report,
    })

    # Fastest path: if the fully validated input signature already has a cached
    # StarDict+QA bundle, no parsed SQLite stage is needed at all.  4.4 restored a
    # ~1-GiB form database first and only then discovered that export artifacts
    # were reusable.  4.5 checks the final artifact before touching that database.
    artifact_cache = ArtifactCache(cache_dir)
    artifact_name = f"stardict-{profile_name}"
    if stage_enabled and not args.rebuild_stage_cache and not args.no_artifact_cache:
        t0 = time.perf_counter()
        early_bundle = artifact_cache.restore(artifact_name, export_sig, out_dir)
        elapsed = time.perf_counter() - t0
        if isinstance(early_bundle, dict):
            cached_result = early_bundle.get("stardict")
            cached_database = early_bundle.get("database")
            if isinstance(cached_result, dict) and isinstance(cached_database, dict):
                timings.mark("Восстановление готового StarDict", elapsed, cached=True)
                print(f"[КЭШ STARDICT] {artifact_name}: восстановлен напрямую за {elapsed:.1f} с")

                cached_sources = early_bundle.get("sources")
                if isinstance(cached_sources, dict):
                    source_stats.update(cached_sources)
                    _source_stats_report(cached_sources)
                cached_cleanup = early_bundle.get("quality_cleanup")
                if isinstance(cached_cleanup, dict):
                    build_stats["quality_cleanup"] = cached_cleanup
                    report.semantic_cleanup(cached_cleanup)
                cached_resolve = early_bundle.get("resolve")
                if isinstance(cached_resolve, dict):
                    build_stats["resolve"] = cached_resolve
                    report.resolve(cached_resolve)
                cached_form = early_bundle.get("form_display_quality")
                if isinstance(cached_form, dict):
                    build_stats["form_display_quality"] = cached_form
                    report.form_quality(cached_form)
                cached_quality = early_bundle.get("quality")
                if isinstance(cached_quality, dict) and cached_quality and not args.no_quality_report:
                    build_stats["quality"] = cached_quality
                    report.quality(cached_quality)

                build_stats["database"] = cached_database
                report.database_totals(
                    cached_database.get("lemmas", 0),
                    cached_database.get("definitions", 0),
                    cached_database.get("lookup_keys_before_output", 0),
                )
                build_stats["stardict"] = cached_result
                report.stardict(cached_result)
                build_stats["timings"] = {
                    "stages": timings.items,
                    "total_seconds": round(timings.total(), 3),
                    "restored_stage": "artifact",
                }
                report.performance(build_stats["timings"])
                (out_dir / "BUILD_STATS.json").write_text(
                    json.dumps(build_stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                print(f"\nПодробная машинная статистика: {out_dir / 'BUILD_STATS.json'}")
                _source_info(cache_dir, out_dir, cached_result)
                save_progress_totals()
                return 0

    conn: sqlite3.Connection | None = None
    restored_level = ""
    restored_stats: dict[str, object] = {}
    if stage_enabled and not args.rebuild_stage_cache:
        candidates = [
            (f"form-{profile_name}", form_sig, "form"),
            (f"resolved-{profile_name}", resolved_sig, "resolved"),
            (f"clean-{profile_name}", quality_sig, "clean"),
        ]
        if max_sig:
            candidates.append(("max", max_sig, "max"))
        candidates.append(("lexical", lexical_sig, "lexical"))
        for cache_name, sig, level in candidates:
            t0 = time.perf_counter()
            restored = _restore_stage(stage_cache, cache_name, sig, db_path)
            if restored is not None:
                conn, restored_stats = restored
                timings.mark(f"Восстановление кэша {cache_name}", time.perf_counter() - t0, cached=True)
                restored_level = level
                break

    if conn is None:
        conn = connect_db(db_path)

    # Recover the human/source statistics stored with a parsed stage.
    if restored_stats:
        rs = restored_stats.get("sources")
        if isinstance(rs, dict):
            source_stats.update(rs)
            _source_stats_report(rs)
        rr = restored_stats.get("resolve")
        if isinstance(rr, dict):
            build_stats["resolve"] = rr
        rq = restored_stats.get("quality_cleanup")
        if isinstance(rq, dict):
            build_stats["quality_cleanup"] = rq
        rf = restored_stats.get("form_display_quality")
        if isinstance(rf, dict):
            build_stats["form_display_quality"] = rf

    # -------------------------- lexical layer --------------------------
    if restored_level not in {"lexical", "max", "clean", "resolved", "form"}:
        stats = timings.run(
            "Wiktionary / Kaikki", process_kaikki,
            kaikki, conn, langs, include_forms=not args.no_forms,
            max_records=args.max_records, **flags,
        )
        source_stats["wiktionary"] = stats
        report.source_wiktionary(stats)

        if wikidata_lexemes and wikidata_lexemes.exists():
            stats = timings.run(
                "Wikidata Lexemes", process_wikidata_lexemes,
                wikidata_lexemes, conn,
                fallback_only=not args.wikidata_merge,
                include_forms=not args.no_forms,
                max_records=args.max_records, **flags,
            )
            source_stats["wikidata_lexemes"] = stats
            report.source_wikidata(stats)

        if dal and dal.exists():
            stats = timings.run(
                "Словарь Даля", process_legacy_stardict,
                dal, conn, source_name="dal", profile="dal",
                fallback_only=not args.dal_merge, **flags,
            )
            source_stats["dal"] = stats
            report.source_dal(stats)

        for legacy in legacy_paths:
            stats = timings.run(
                f"Доп. StarDict {legacy.name}", process_legacy_stardict,
                legacy, conn, source_name=f"legacy:{legacy.name}", profile="generic",
                fallback_only=True, **flags,
            )
            source_stats[f"legacy:{legacy.name}"] = stats
            report.section(f"ДОПОЛНИТЕЛЬНЫЙ STARDICT — {legacy.name}", [
                ("Исходных статей", report.fmt_int(stats.get("source_entries", 0))),
                ("Добавлено определений", report.fmt_int(stats.get("definitions_added", 0))),
                ("Пропущено: уже определены", report.fmt_int(stats.get("skipped_existing", 0))),
                ("Отбраковано", report.fmt_int(stats.get("rejected", 0))),
                ("Добавлено синонимов", report.fmt_int(stats.get("synonyms_added", 0))),
            ])

        for path in extra_tsv + auto_tsv:
            if path.exists():
                added = timings.run(f"TSV {path.name}", process_extra_tsv, path, conn, **flags)
                report.added_source("ДОПОЛНИТЕЛЬНЫЕ ТЕРМИНЫ / TSV", added, path=path)
        for path in extra_jsonl + auto_jsonl:
            if path.exists():
                added = timings.run(f"JSONL {path.name}", process_extra_jsonl, path, conn, **flags)
                report.added_source("ДОПОЛНИТЕЛЬНЫЕ ТЕРМИНЫ / JSONL", added, path=path)

        if stage_enabled:
            _save_stage(stage_cache, "lexical", lexical_sig, db_path, conn, {"sources": dict(source_stats)})

    # -------------------------- Wikipedia layer ------------------------
    if wikipedia and wikipedia.exists() and restored_level not in {"max", "clean", "resolved", "form"}:
        stats = timings.run(
            "Wikipedia terminology", process_wikipedia_terminology,
            wikipedia, conn,
            fallback_only=not args.wikipedia_merge,
            quality_upgrade_existing=args.wikipedia_quality_upgrade,
            max_pages=args.max_wikipedia_pages, **flags,
        )
        source_stats["wikipedia"] = stats
        report.source_wikipedia(stats)
        if stage_enabled and max_sig:
            _save_stage(stage_cache, "max", max_sig, db_path, conn, {"sources": dict(source_stats)})

    # -------------------------- semantic quality -----------------------
    if restored_level not in {"clean", "resolved", "form"}:
        cleanup_stats = timings.run("Семантическая очистка", semantic_quality_pass, conn, **flags)
        prepared_for_rescue = _wiki_prepared_cache_path(wikipedia) if wikipedia and wikipedia.exists() else None
        if prepared_for_rescue is not None and prepared_for_rescue.exists():
            rescue_stats = timings.run(
                "Точечное улучшение из Wikipedia",
                wikipedia_quality_rescue, prepared_for_rescue, conn, **flags,
            )
        else:
            # semantic_quality_pass creates this work table even for lexical-only
            # builds. Do not leak it into the persistent clean cache.
            rescue_stats = wikipedia_quality_rescue(None, conn, **flags)
        for key, value in rescue_stats.items():
            cleanup_stats[f"wikipedia_rescue_{key}"] = value
        build_stats["quality_cleanup"] = cleanup_stats
        report.semantic_cleanup(cleanup_stats)
        if stage_enabled:
            _save_stage(
                stage_cache, f"clean-{profile_name}", quality_sig, db_path, conn,
                {"sources": dict(source_stats), "quality_cleanup": cleanup_stats},
            )
    else:
        cleanup_stats = build_stats.get("quality_cleanup") or {}
        if isinstance(cleanup_stats, dict):
            report.semantic_cleanup(cleanup_stats)

    # -------------------------- graph resolution -----------------------
    if restored_level not in {"resolved", "form"}:
        resolve_stats = timings.run("Разрешение ссылок", resolve_links, conn)
        build_stats["resolve"] = resolve_stats
        report.resolve(resolve_stats)
        if stage_enabled:
            _save_stage(
                stage_cache, f"resolved-{profile_name}", resolved_sig, db_path, conn,
                {"sources": dict(source_stats), "quality_cleanup": build_stats.get("quality_cleanup", {}), "resolve": resolve_stats},
            )
    else:
        resolve_stats = build_stats.get("resolve") or {}
        if isinstance(resolve_stats, dict): report.resolve(resolve_stats)

    # -------------------------- display semantics ----------------------
    if restored_level != "form":
        form_quality = timings.run("Естественные значения словоформ", materialize_form_overrides, conn)
        build_stats["form_display_quality"] = form_quality
        report.form_quality(form_quality)
        if stage_enabled:
            _save_stage(
                stage_cache, f"form-{profile_name}", form_sig, db_path, conn,
                {"sources": dict(source_stats), "quality_cleanup": build_stats.get("quality_cleanup", {}),
                 "resolve": build_stats.get("resolve", {}), "form_display_quality": form_quality},
            )
    else:
        form_quality = build_stats.get("form_display_quality") or {}
        if isinstance(form_quality, dict): report.form_quality(form_quality)

    counts = conn.execute(
        "SELECT (SELECT COUNT(DISTINCT lemma) FROM senses), (SELECT COUNT(*) FROM senses), (SELECT COUNT(DISTINCT key) FROM links)"
    ).fetchone()
    database_stats = {"lemmas": counts[0], "definitions": counts[1], "lookup_keys_before_output": counts[2]}
    build_stats["database"] = database_stats

    # Final artifacts are cached together with the QA reports. On an unchanged
    # form-stage, a forced rebuild can therefore avoid both the ~7 s quality audit
    # and the much more expensive 6+ million-key StarDict export.
    result: dict[str, object] | None = None
    quality_summary: dict[str, object] | None = None
    artifact_bundle: dict[str, object] | None = None
    artifact_restored = False
    if stage_enabled and not args.rebuild_stage_cache and not args.no_artifact_cache:
        t0 = time.perf_counter()
        cached_bundle = artifact_cache.restore(artifact_name, export_sig, out_dir)
        if cached_bundle is not None:
            elapsed = time.perf_counter() - t0
            timings.mark("Восстановление готового StarDict", elapsed, cached=True)
            print(f"[КЭШ STARDICT] {artifact_name}: восстановлен за {elapsed:.1f} с")
            artifact_restored = True
            artifact_bundle = cached_bundle
            nested = cached_bundle.get("stardict") if isinstance(cached_bundle, dict) else None
            if isinstance(nested, dict):
                result = nested
                cq = cached_bundle.get("quality")
                if isinstance(cq, dict) and cq:
                    quality_summary = cq
            elif isinstance(cached_bundle, dict):
                # Compatibility with the first internal artifact-cache prototype.
                result = cached_bundle

    if not args.no_quality_report:
        if quality_summary is None:
            quality = timings.run("Контроль качества", write_quality_report, conn, out_dir)
            quality_summary = {k: v for k, v in quality.items() if k != "examples"}
        build_stats["quality"] = quality_summary
        report.quality(quality_summary)

    report.database_totals(counts[0], counts[1], counts[2])

    if result is None:
        result = timings.run("Экспорт StarDict", build_stardict, conn, out_dir)

    # Save/upgrade the final bundle after both QA and export exist. If the bundle
    # was restored with cached QA, no rewrite is needed.
    need_artifact_save = (
        stage_enabled and not args.no_artifact_cache and
        (not artifact_restored or (not args.no_quality_report and not isinstance(artifact_bundle, dict))
         or (not args.no_quality_report and quality_summary is not None and
             not (isinstance(artifact_bundle, dict) and isinstance(artifact_bundle.get("quality"), dict))))
    )
    if need_artifact_save:
        t0 = time.perf_counter()
        extras = []
        if not args.no_quality_report:
            extras = [
                out_dir / "QUALITY_REPORT.json",
                out_dir / "QUALITY_REPORT.txt",
                out_dir / "QUALITY_REVIEW.tsv",
                out_dir / "QUALITY_ONOMASTICS.tsv",
                out_dir / "QUALITY_CONCISE.tsv",
            ]
        artifact_cache.save(
            artifact_name, export_sig, out_dir / "ru-max-clean",
            stats={
                "stardict": result,
                "quality": quality_summary or {},
                "database": database_stats,
                "sources": dict(source_stats),
                "quality_cleanup": build_stats.get("quality_cleanup", {}),
                "resolve": build_stats.get("resolve", {}),
                "form_display_quality": build_stats.get("form_display_quality", {}),
            },
            extra_files=extras,
        )
        elapsed = time.perf_counter() - t0
        timings.mark("Сохранение кэша StarDict", elapsed, cached=True)
        print(f"[КЭШ STARDICT] {artifact_name}: сохранён ({artifact_cache.describe(artifact_name)}) за {elapsed:.1f} с")

    build_stats["stardict"] = result
    report.stardict(result)
    build_stats["timings"] = {
        "stages": timings.items,
        "total_seconds": round(timings.total(), 3),
        "restored_stage": restored_level or None,
    }
    report.performance(build_stats["timings"])
    (out_dir / "BUILD_STATS.json").write_text(
        json.dumps(build_stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\nПодробная машинная статистика: {out_dir / 'BUILD_STATS.json'}")
    _source_info(cache_dir, out_dir, result)
    save_progress_totals()
    conn.close()
    if not args.keep_db and db_path.exists():
        db_path.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
