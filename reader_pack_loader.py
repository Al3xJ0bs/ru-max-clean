"""Small public loader for optional reader-pack TSV files.

The corpus scanner lives in an internal module and is deliberately not shipped
in builder-only releases.  Keeping this loader independent lets users build the
selected literary/language packs without receiving book-analysis tooling.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import unicodedata


# E-books often flatten accents during conversion and may replace a straight
# apostrophe with a typographic one.  In a foreign-language companion layer
# those variants denote the same printed form, so keeping both is a safe
# lookup aid.  This deliberately does *not* transliterate between alphabets or
# guess grammatical forms.
_APOSTROPHE_FORMS = ("'", "\u2019")
_LIGATURES = str.maketrans({"\u00e6": "ae", "\u00c6": "AE", "\u0153": "oe", "\u0152": "OE"})


def _accentless(value: str) -> str:
    """Return a spelling without Latin diacritics, preserving all other text."""

    value = value.translate(_LIGATURES)
    decomposed = unicodedata.normalize("NFD", value)
    result: list[str] = []
    previous_is_latin = False
    for char in decomposed:
        if unicodedata.combining(char):
            if previous_is_latin:
                continue
        else:
            previous_is_latin = "LATIN" in unicodedata.name(char, "")
        result.append(char)
    return "".join(result)


def _lookup_variants(value: str) -> tuple[str, ...]:
    """Create only typographic equivalents suitable for reader lookups.

    For example, ``s'il vous plaît`` yields ``s’il vous plaît`` and the two
    accentless forms.  The source spelling itself is intentionally omitted;
    callers already store it as the primary key.
    """

    variants = {value}
    pending = [value]
    while pending:
        current = pending.pop()
        candidates = {_accentless(current)}
        if any(mark in current for mark in _APOSTROPHE_FORMS):
            for mark in _APOSTROPHE_FORMS:
                candidates.add(current.replace("'", mark).replace("\u2019", mark))
        for candidate in candidates:
            if candidate and candidate not in variants:
                variants.add(candidate)
                pending.append(candidate)
    return tuple(sorted(candidate for candidate in variants if candidate != value))


@dataclass(frozen=True)
class PackEntry:
    word: str
    definition: str
    aliases: tuple[str, ...] = ()


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
            if len(fields) > 3:
                raise ValueError(f"{path.name}:{line_no}: expected at most three tab-separated fields")
            word = fields[0].strip()
            explicit_aliases = (
                tuple(item.strip() for item in fields[2].split("|") if item.strip())
                if len(fields) == 3 else ()
            )
            # Preserve author-supplied aliases first, then append deterministic
            # typographic forms without duplicates.  They remain in the
            # companion pack only and therefore cannot affect RU-Max-Clean.
            aliases = tuple(dict.fromkeys((*explicit_aliases, *_lookup_variants(word))))
            entries.append(PackEntry(word, fields[1].strip(), aliases))
    return entries
