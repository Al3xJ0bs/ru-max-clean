#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import struct
import sys
from pathlib import Path
import human_report as report

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


def contains_probable_html(text: str) -> bool:
    # Do not flag angle-bracket notation such as Russian Wiktionary's
    # "<и> — <э>" as HTML. Only recognizable markup patterns count.
    return bool(
        HTML_PAIR_RE.search(text)
        or HTML_CLOSE_RE.search(text)
        or HTML_VOID_RE.search(text)
        or HTML_ATTR_RE.search(text)
    )



# Same visible metadata labels stripped by the builder. If one survives at the
# start of a meaning, the output is not definition-only.
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
LEAKED_LABEL_RE = re.compile(
    rf"(?im)^(?:\d+\.\s*)?(?:(?:[\[(](?:{LABEL_TOKEN})(?:\s*[,;/]\s*(?:{LABEL_TOKEN}))*[\])])|"
    rf"(?:(?:{LABEL_TOKEN})(?:\s*[,;/]\s*(?:{LABEL_TOKEN}))*))(?:\s*[:;,—-]?\s*)"
)

LEAKED_DOMAIN_CONTEXT_RE = re.compile(
    r"(?im)^(?:\d+\.\s*)?[\[(](?:\u0432|\u0432\u043e|\u0434\u043b\u044f|\u043f\u0440\u0438|\u0432\s+\u043e\u0431\u043b\u0430\u0441\u0442\u0438|\u0432\s+\u0441\u0444\u0435\u0440\u0435)\b[^\])]{0,90}"
    r"(?:\u0437\u0435\u043c\u0435\u043b\u044c\u043d|\u043f\u0440\u0430\u0432|\u044e\u0440\u0438\u0434|\u0444\u0438\u0437\u0438\u043a|\u043c\u0430\u0442\u0435\u043c\u0430\u0442|\u0445\u0438\u043c|\u043c\u0435\u0434\u0438\u0446|\u0431\u0438\u043e\u043b\u043e\u0433|\u0442\u0435\u0445\u043d\u0438\u043a|\u044d\u043b\u0435\u043a\u0442\u0440|\u043c\u0435\u0445\u0430\u043d|\u0438\u043d\u0444\u043e\u0440\u043c\u0430\u0442|\u044d\u043a\u043e\u043d\u043e\u043c|\u0444\u0438\u043d\u0430\u043d\u0441|\u0433\u0435\u043e\u043b|\u0441\u0442\u0440\u043e\u0438\u0442|\u0432\u043e\u0435\u043d|\u043b\u0438\u043d\u0433\u0432)[^\])]{0,60}[\])]\s*",
    re.IGNORECASE,
)

GRAMMAR_ONLY_RE = re.compile(
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

FORM_TARGET_WORD = r"[A-Za-zА-Яа-яЁёІіѢѣѲѳѴѵ]+(?:-[A-Za-zА-Яа-яЁёІіѢѣѲѳѴѵ]+)*"
ABBREV_GRAMMAR_ONLY_RE = re.compile(
    rf"^\s*(?:(?:страд(?:ат(?:ельн(?:ое|ый|ая)?)?)?|действ(?:ительн(?:ое|ый|ая)?)?)\.?\s+)?"
    rf"(?:прич(?:астие)?|деепр(?:ичастие)?)\.?"
    rf"(?:\s+(?:прош(?:едш(?:его|ее|ий)?)?|наст(?:оящ(?:его|ее|ий)?)?|буд(?:ущ(?:его|ее|ий)?)?)\.?)?"
    rf"(?:\s+(?:вр(?:емени)?|времени)\.?)?"
    rf"(?:\s+(?:кратк(?:ая|ое|ие)?|полн(?:ая|ое|ые)?)\.?)?"
    rf"\s+от\s+(?:(?:гл(?:агола)?|слова|лексемы)\.?\s+)?"
    rf"[«\"']?{FORM_TARGET_WORD}[»\"']?\s*[.!]?\s*$",
    re.IGNORECASE,
)

# Definite non-meaning residue. These patterns are intentionally much narrower
# than the heuristic QUALITY_REPORT: the strict validator fails only on content
# that should never be visible in a definition-only KOReader popup.
LEAKED_EXAMPLE_OR_WIKI_RE = re.compile(r"(?:[◆◇]|\[НКРЯ\]|\{\{|\[\[)", re.IGNORECASE)
PLACEHOLDER_ONLY_RE = re.compile(r"^(?:сокр(?:ащение)?\.?|аббр\.?|имя\s+собственное\.?)$", re.IGNORECASE)
FRAGMENT_ONLY_RE = re.compile(
    r"^(?:гора|город|река|деревня|село|пос[ёе]лок|округ|область)\s+(?:в|на|из)$",
    re.IGNORECASE,
)

def leaked_nonmeaning_line(text: str) -> str | None:
    for line in text.splitlines():
        body = re.sub(r"^\s*\d+\.\s*", "", line).strip()
        if not body:
            continue
        if LEAKED_EXAMPLE_OR_WIKI_RE.search(body):
            return body
        if PLACEHOLDER_ONLY_RE.fullmatch(body) or FRAGMENT_ONLY_RE.fullmatch(body):
            return body
    return None


def leaked_grammar_line(text: str) -> str | None:
    """Return an obvious form-description line if one survived the builder."""
    for line in text.splitlines():
        body = re.sub(r"^\s*\d+\.\s*", "", line).strip()
        if not body:
            continue
        if GRAMMAR_ONLY_RE.fullmatch(body) or ABBREV_GRAMMAR_ONLY_RE.fullmatch(body):
            return body
    return None



def parse_ifo(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if not lines or lines[0] != "StarDict's dict ifo file":
        raise ValueError("invalid StarDict .ifo header")
    for line in lines[1:]:
        if "=" in line:
            k, v = line.split("=", 1)
            data[k] = v
    return data


def read_idx(path: Path):
    raw = path.read_bytes()
    pos = 0
    entries = []
    while pos < len(raw):
        z = raw.find(b"\0", pos)
        if z < 0 or z + 9 > len(raw):
            raise ValueError(f"broken .idx near byte {pos}")
        word_b = raw[pos:z]
        try:
            word = word_b.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ValueError(f"invalid UTF-8 index key near byte {pos}: {e}") from e
        off, size = struct.unpack(">II", raw[z + 1:z + 9])
        entries.append((word, word_b, off, size))
        pos = z + 9
    return entries


def validate(base: Path, strict_meanings: bool = True) -> dict[str, int]:
    ifo = base.with_suffix(".ifo")
    idx = base.with_suffix(".idx")
    dic = base.with_suffix(".dict")
    for p in (ifo, idx, dic):
        if not p.exists():
            raise FileNotFoundError(p)

    meta = parse_ifo(ifo)
    entries = read_idx(idx)
    dict_size = dic.stat().st_size
    expected = int(meta.get("wordcount", "-1"))
    if expected != len(entries):
        raise ValueError(f"wordcount mismatch: ifo={expected}, idx={len(entries)}")
    if int(meta.get("idxfilesize", "-1")) != idx.stat().st_size:
        raise ValueError("idxfilesize mismatch")

    previous = None
    pointers = set()
    with dic.open("rb") as f:
        checked_articles = set()
        for word, word_b, off, size in entries:
            if previous is not None and previous > word_b:
                raise ValueError(f"index not byte-sorted: {word!r}")
            previous = word_b
            if off + size > dict_size:
                raise ValueError(f"out-of-range article pointer for {word!r}")
            pointers.add((off, size))
            if strict_meanings and (off, size) not in checked_articles:
                f.seek(off)
                text = f.read(size).decode("utf-8")
                if contains_probable_html(text):
                    raise ValueError(f"HTML leaked into article for {word!r}: {text[:160]!r}")
                # Detect only obvious form-description articles.  Merely containing
                # a grammatical phrase is not an error: for a lexical term such as
                # "датив", "дательный падеж" is the actual meaning.
                bad_grammar = leaked_grammar_line(text)
                if bad_grammar:
                    raise ValueError(f"grammar-only line leaked for {word!r}: {bad_grammar[:120]!r}")
                bad_nonmeaning = leaked_nonmeaning_line(text)
                if bad_nonmeaning:
                    raise ValueError(f"non-meaning residue leaked for {word!r}: {bad_nonmeaning[:120]!r}")
                if LEAKED_LABEL_RE.search(text):
                    raise ValueError(f"metadata label leaked for {word!r}: {text[:120]!r}")
                if LEAKED_DOMAIN_CONTEXT_RE.search(text):
                    raise ValueError(f"domain parenthetical leaked for {word!r}: {text[:120]!r}")
                checked_articles.add((off, size))
    return {
        "wordcount": len(entries),
        "unique_article_pointers": len(pointers),
        "idx_bytes": idx.stat().st_size,
        "dict_bytes": dict_size,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("base", help="Path without .ifo/.idx/.dict suffix")
    p.add_argument("--allow-grammar", action="store_true")
    args = p.parse_args(argv)
    stats = validate(Path(args.base), strict_meanings=not args.allow_grammar)
    report.validation(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
