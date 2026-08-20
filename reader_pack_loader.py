"""Small public loader for optional reader-pack TSV files.

The corpus scanner lives in an internal module and is deliberately not shipped
in builder-only releases.  Keeping this loader independent lets users build the
selected literary/language packs without receiving book-analysis tooling.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
            aliases = tuple(item.strip() for item in fields[2].split("|") if item.strip()) if len(fields) >= 3 else ()
            entries.append(PackEntry(fields[0].strip(), fields[1].strip(), aliases))
    return entries
