Reader-layer source packs
=========================

latin_classical.tsv, literary_archaic.tsv, and phraseology.tsv are curated seed
records maintained with this project.

latin_wiktionary.tsv and literary_wiktionary.tsv are extracted from the local
Russian Wiktionary/Kaikki dump.  They retain Russian glosses only; forms,
examples, placeholders, and obvious metadata are filtered by
extract_wiktionary_pack.py.  Check the upstream Wiktionary/Kaikki license and
attribution requirements before redistribution.

Generated StarDict files are deliberately not stored here.  Build them with
build_reader_packs.py into a separate output directory.
