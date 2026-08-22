Reader-layer source packs
=========================

latin_classical.tsv, literary_archaic.tsv, and phraseology.tsv are curated seed
records maintained with this project.

french_literary.tsv is a curated French companion layer for French passages,
forms of address, titles, and common expressions in Russian classical prose. It
is deliberately separate from the Latin packs because short French function
words can collide with Latin keys.

german_literary.tsv is a separate German companion layer for dialogue,
military commands, forms of address, and short quotations.  Its initial set is
grounded in the supplied Russian editions of Ha\u0161ek, Kafka, and Goethe; it
also stays separate because common German particles should never become entries
in the Russian core dictionary.

literary_names.tsv contains curated characters, historical figures, and places
from the literary corpus. fantasy_terms.tsv contains short explanations of
world-specific fantasy vocabulary. Both layers remain optional and are kept
outside the Russian core to avoid treating proper names as ordinary Russian
definitions.

literary_terms.tsv holds a small separate layer for historical and
culture-specific words such as military, Spanish, and medieval titles.

latin_wiktionary.tsv and literary_wiktionary.tsv are extracted from the local
Russian Wiktionary/Kaikki dump.  They retain Russian glosses only; forms,
examples, placeholders, and obvious metadata are filtered by
extract_wiktionary_pack.py.  Check the upstream Wiktionary/Kaikki license and
attribution requirements before redistribution.

Generated StarDict files are deliberately not stored here.  Build them with
build_reader_packs.py; by default they go to
RU-Dictionaries/RU-Reader-Packs/<pack>/. An explicit --output-dir still allows
older installations to keep using a top-level RU-Reader-Packs directory.

The folder slugs stay stable for scripts and upgrades.  The generated StarDict
titles shown by KOReader are reader-friendly Russian names rather than raw
filenames (for example, "Латынь — расширенный словарь" and
"Литературные сокращения").
