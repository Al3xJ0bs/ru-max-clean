#!/bin/sh
set -eu
cd "$(dirname "$0")"
PYTHON=${PYTHON:-python3}
"$PYTHON" build_ru_max_clean.py \
  --download-kaikki \
  --download-wikidata-lexemes \
  --download-dal \
  --download-wikipedia \
  --wikipedia-quality-upgrade \
  --output-dir RU-Dictionaries/RU-Max-Clean
"$PYTHON" validate_stardict.py RU-Dictionaries/RU-Max-Clean/ru-max-clean
echo "Done. Copy RU-Dictionaries/RU-Max-Clean to koreader/data/dict/"
