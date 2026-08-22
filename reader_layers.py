#!/usr/bin/env python3
"""Reader-oriented optional layers for RU Max Clean.

The core dictionary deliberately remains Russian-only.  This module provides
two small, reusable primitives for companion dictionaries:

* a conservative TSV pack loader used by ``build_reader_packs.py``;
* a book-coverage scanner that reports frequent words absent from a StarDict
  index, grouped by writing system.

No network access and no source-specific assumptions live here.  That keeps
optional literary/Latin layers independently testable and lets future packs be
added without invalidating the expensive RU-Max-Clean caches.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import gzip
import html
import json
from html.parser import HTMLParser
from pathlib import Path
import re
import struct
import unicodedata
import zipfile
from typing import Iterable, Iterator, Sequence


# Keep hyphenated and apostrophised book words together (de-facto, l'homme),
# while excluding punctuation and underscores.  The scanner is intentionally
# lexical: it is a prioritisation tool, not a morphological analyser.
TOKEN_RE = re.compile(r"[^\W_]+(?:[-'’][^\W_]+)*", re.UNICODE)
WS_RE = re.compile(r"\s+")
DASHES = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
})


@dataclass(frozen=True)
class PackEntry:
    word: str
    definition: str
    aliases: tuple[str, ...] = ()


class _TextHTMLParser(HTMLParser):
    # FB2 files commonly embed cover images as megabytes of base64 inside
    # <binary>.  Metadata sections are not book prose either.  Skipping them
    # prevents apparent Latin "words" such as wA/Pj4 from polluting coverage.
    SKIP_TAGS = {
        "binary", "stylesheet", "description", "coverpage", "title-info",
        "src-title-info", "document-info", "publish-info", "custom-info",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        if tag.casefold().split(":")[-1] in self.SKIP_TAGS:
            self._skip_depth += 1

    def handle_startendtag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        # A self-closing skipped element does not change depth.
        return

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth and tag.casefold().split(":")[-1] in self.SKIP_TAGS:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        return " ".join(self.parts)


def normalize_lookup(value: str) -> str:
    """Normalise a book token in the same conservative way as lookup aliases."""
    value = unicodedata.normalize("NFC", value).translate(DASHES)
    value = WS_RE.sub(" ", value.strip()).casefold()
    # Russian books freely mix ё/е; matching both is useful for coverage and
    # does not alter the original token shown in the report.
    return value.replace("ё", "е")


def script_of(token: str) -> str:
    has_cyr = any("\u0400" <= ch <= "\u052f" for ch in token)
    has_lat = any(("A" <= ch <= "Z") or ("a" <= ch <= "z") or
                  (0x00c0 <= ord(ch) <= 0x024f) or
                  (0x1e00 <= ord(ch) <= 0x1eff) for ch in token)
    has_greek = any("\u0370" <= ch <= "\u03ff" for ch in token)
    has_other = any(ch.isalpha() and not (has_cyr or has_lat or has_greek)
                    for ch in token)
    if sum((has_cyr, has_lat, has_greek, has_other)) > 1:
        return "mixed"
    if has_cyr:
        return "cyrillic"
    if has_lat:
        return "latin"
    if has_greek:
        return "greek"
    if has_other:
        return "other-alphabet"
    return "numeric"


def iter_tokens(text: str) -> Iterator[str]:
    yield from TOKEN_RE.findall(text)


def iter_lookup_candidates(text: str, max_words: int = 4) -> Iterator[str]:
    """Yield normalised single words and short contiguous phrases."""
    tokens = [normalize_lookup(token) for token in iter_tokens(text)]
    for index, token in enumerate(tokens):
        if token:
            yield token
        for width in range(2, min(max_words, len(tokens) - index) + 1):
            phrase = " ".join(tokens[index:index + width])
            if phrase.strip():
                yield phrase


def _read_html(data: str) -> str:
    # ``html.parser`` treats <title> as a raw-text element in some Python
    # versions. FB2 uses <title> as a normal container with nested <p> tags;
    # without this neutralisation the literal tag name ``p`` leaks into the
    # coverage report hundreds of times.
    data = re.sub(r"<(/?)title(?=[\s>])", r"<\1fb-title", data, flags=re.IGNORECASE)
    parser = _TextHTMLParser()
    parser.feed(data)
    parser.close()
    return html.unescape(parser.text())


def read_book_text(path: Path) -> str:
    """Read UTF-8 text, HTML, EPUB, or gzip-wrapped text for coverage scans."""
    suffix = path.suffix.casefold()
    if suffix == ".epub":
        parts: list[str] = []
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                name = info.filename.casefold()
                if name.endswith((".xhtml", ".html", ".htm")):
                    raw = archive.read(info).decode("utf-8", errors="replace")
                    parts.append(_read_html(raw))
        return "\n".join(parts)
    opener = gzip.open if suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as stream:
        data = stream.read()
    if suffix in {".html", ".htm", ".xhtml", ".fb2"}:
        return _read_html(data)
    return data


class KnownKeyMatcher:
    """Small normalised key set used after book tokens are collected."""

    def __init__(self, keys: Iterable[str] = ()) -> None:
        self.keys = {normalize_lookup(key) for key in keys if normalize_lookup(key)}
        self.phrase_keys = {
            key for key in self.keys if " " in key
        }
        self.single_keys = self.keys - self.phrase_keys

    def __contains__(self, token: str) -> bool:
        return normalize_lookup(token) in self.keys

    def phrase_hits(self, normalized_tokens: Sequence[str], max_words: int = 4) -> Counter[str]:
        """Return component-token counts covered by known multiword keys."""
        covered: Counter[str] = Counter()
        if not self.phrase_keys:
            return covered
        for start in range(len(normalized_tokens)):
            for width in range(2, min(max_words, len(normalized_tokens) - start) + 1):
                phrase = " ".join(normalized_tokens[start:start + width])
                if phrase in self.phrase_keys:
                    covered.update(normalized_tokens[start:start + width])
        return covered


def iter_stardict_keys(index_path: Path) -> Iterator[str]:
    """Stream keys from a StarDict 2.4.2 ``.idx`` file without loading it."""
    # ``.idx`` contains ``UTF-8 key + NUL + 8-byte metadata`` records.  The
    # former byte-at-a-time reader made an ordinary coverage scan over the
    # 200 MiB core index take several minutes.  Buffered chunks preserve the
    # streaming/memory-bounded behaviour while avoiding millions of tiny I/O
    # calls.  Keep an incomplete record in ``pending`` for the next chunk.
    with index_path.open("rb") as stream:
        pending = b""
        while chunk := stream.read(1024 * 1024):
            data = pending + chunk
            offset = 0
            while True:
                nul = data.find(b"\0", offset)
                if nul < 0 or len(data) < nul + 9:
                    pending = data[offset:]
                    break
                raw = data[offset:nul]
                offset = nul + 9
                try:
                    yield raw.decode("utf-8")
                except UnicodeDecodeError:
                    # A malformed external index must not abort a coverage scan.
                    continue
        # A partial final record is invalid; ignore it like the old reader.


def matcher_from_stardict(index_path: Path, observed: Iterable[str], *,
                          collect_short_phrases: bool = False) -> KnownKeyMatcher:
    """Find observed keys in a large index without loading all its entries.

    ``collect_short_phrases`` keeps two-to-four-word keys as well.  It is used
    by the book-coverage tool after it has collected only single tokens: that
    avoids materialising every possible n-gram from a multi-million-token
    corpus while preserving phrase coverage exactly.
    """
    wanted = {normalize_lookup(token) for token in observed if normalize_lookup(token)}
    found: set[str] = set()
    if not wanted:
        return KnownKeyMatcher()
    for key in iter_stardict_keys(index_path):
        normal = normalize_lookup(key)
        if normal in wanted:
            found.add(normal)
            if not collect_short_phrases and len(found) == len(wanted):
                break
        elif collect_short_phrases and 1 <= normal.count(" ") <= 3:
            found.add(normal)
    return KnownKeyMatcher(found)


def _counter_payload(counter: Counter[str], raw_forms: dict[str, Counter[str]],
                     matcher: KnownKeyMatcher, phrase_covered: Counter[str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for normal, count in counter.most_common():
        remaining = count - (count if normal in matcher.single_keys else 0) - phrase_covered.get(normal, 0)
        if remaining <= 0:
            continue
        forms = raw_forms[normal]
        raw, _ = forms.most_common(1)[0]
        rows.append({
            "token": raw,
            "normalized": normal,
            "count": remaining,
            "script": script_of(raw),
            "variants": [item for item, _n in forms.most_common(5)],
        })
    return rows


def scan_coverage(texts: Sequence[tuple[str, str]], matcher: KnownKeyMatcher) -> dict[str, object]:
    """Return a deterministic, JSON-serialisable coverage report.

    ``texts`` contains ``(label, text)`` pairs so a caller can scan multiple
    books without losing per-file totals.  Unknown rows are sorted by
    descending frequency and then by normalised spelling.
    """
    counts: Counter[str] = Counter()
    phrase_covered: Counter[str] = Counter()
    raw_forms: dict[str, Counter[str]] = defaultdict(Counter)
    scripts: Counter[str] = Counter()
    file_stats: list[dict[str, object]] = []
    for label, text in texts:
        local: Counter[str] = Counter()
        tokens = list(iter_tokens(text))
        normalized_tokens = [normalize_lookup(token) for token in tokens]
        local_phrase_covered = matcher.phrase_hits(normalized_tokens)
        phrase_covered.update(local_phrase_covered)
        for token in tokens:
            normal = normalize_lookup(token)
            if not normal or not any(ch.isalpha() for ch in token):
                continue
            local[normal] += 1
            counts[normal] += 1
            raw_forms[normal][token] += 1
            scripts[script_of(token)] += 1
        file_stats.append({
            "file": label,
            "tokens": sum(local.values()),
            "unique_tokens": len(local),
            "unknown_tokens": sum(
                max(0, n - (n if key in matcher.single_keys else 0) - local_phrase_covered.get(key, 0))
                for key, n in local.items()
            ),
        })
    total = sum(counts.values())
    unknown_rows = _counter_payload(counts, raw_forms, matcher, phrase_covered)
    unknown_total = sum(int(row["count"]) for row in unknown_rows)
    # Phrase hits and direct single-word hits overlap frequently (for example,
    # a pack may contain both ``alpha`` and ``alpha beta``).  Derive the known
    # total from the residual unknown count so no token can be counted twice.
    known = total - unknown_total
    unknown_scripts: Counter[str] = Counter()
    for row in unknown_rows:
        unknown_scripts[str(row["script"])] += int(row["count"])
    return {
        "files": file_stats,
        "tokens_total": total,
        "unique_tokens": len(counts),
        "known_tokens": known,
        "unknown_tokens": unknown_total,
        "coverage_percent": round((known / total) * 100, 3) if total else 100.0,
        "script_counts": dict(sorted(scripts.items())),
        "unknown_by_script": dict(sorted(unknown_scripts.items())),
        "unknown": unknown_rows,
    }


def load_pack_tsv(path: Path) -> list[PackEntry]:
    """Load ``word<TAB>definition<TAB>alias1|alias2`` entries."""
    entries: list[PackEntry] = []
    with path.open("r", encoding="utf-8-sig") as stream:
        for line_no, line in enumerate(stream, 1):
            line = line.rstrip("\r\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) < 2 or not fields[0].strip() or not fields[1].strip():
                raise ValueError(f"{path.name}:{line_no}: expected word<TAB>definition[<TAB>aliases]")
            aliases = tuple(item.strip() for item in fields[2].split("|") if item.strip()) if len(fields) >= 3 else ()
            entries.append(PackEntry(fields[0].strip(), fields[1].strip(), aliases))
    return entries


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
