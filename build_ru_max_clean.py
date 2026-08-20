Warning: truncated output (original token count: 62323)
Total output lines: 5232

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
        return False…32323 tokens truncated…         accent_aliases=accent_aliases,
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

