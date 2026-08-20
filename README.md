# RU Max Clean

Русский словарь для KOReader в формате StarDict с отдельными слоями для
литературного чтения. Проект разделяет код сборщика, русское ядро и companion-
пакеты: иностранные выражения, архаика и фразеология не смешиваются с базовым
русским словарём.

Текущая проверенная версия — **4.9.1**.

## Что входит

- потоковая сборка RU-Max-Clean из Wiktionary/Kaikki, Wikidata, Wikipedia и
  словаря Даля;
- семантическая очистка, разрешение ссылок и компактные алиасы словоформ;
- независимые пакеты `latin_classical`, `latin_wiktionary`,
  `literary_archaic`, `literary_wiktionary` и `phraseology`;
- сканер покрытия книг TXT/HTML/EPUB/FB2 с поддержкой коротких фраз;
- регрессионные тесты и строгая проверка StarDict.

## Быстрый старт

Для демонстрационного набора без загрузки больших источников:

```text
py -3 -X utf8 test_builder.py
py -3 -X utf8 test_reader_layers.py
py -3 -X utf8 test_source_manager.py
py -3 -X utf8 test_stage_cache.py
```

Production-сборка использует локальные дампы источников и кэш стадий. Полная
команда описана в `README_RU.txt`; исходные дампы не хранятся в Git.

Сопутствующие пакеты собираются отдельно:

```text
py -3 -X utf8 build_reader_packs.py --pack-dir reader_packs --output-dir RU-Reader-Packs
```

Покрытие книги проверяется так:

```text
py -3 -X utf8 scan_book_coverage.py book.epub --index path\to\ru-max-clean.idx
```

## Качество и релизы

Каждый production-релиз содержит `.ifo/.idx/.dict`, `BUILD_INFO.json`,
`BUILD_STATS.json`, `QUALITY_REPORT.*` и `SOURCES.txt`. В GitHub Releases
публикуются готовые архивы словаря, reader-пакетов и builder; исходный код
остаётся в основной ветке. Перед публикацией выполняются все тесты и
`validate_stardict.py`.

## Лицензии и источники данных

Код распространяется по MIT; см. `LICENSE`. Лицензии и требования атрибуции
для Wiktionary, Wikidata, Wikipedia, OpenCorpora и Даля описаны в
`NOTICE_DATA.txt`. Для каждого бинарного словаря нужно читать `SOURCES.txt`
конкретной сборки и соблюдать условия использованных источников.

## Структура

`build_ru_max_clean.py` — основной builder; `reader_layers.py` и
`build_reader_packs.py` — независимые читательские слои; `test_*.py` — тесты;
`reader_packs/` — TSV-исходники companion-пакетов; `koreader_patch/` —
необязательный Lua-патч для отображения definition-only карточек.

Подробнее о правилах очистки и источниках см. `README_RU.txt`,
`READER_LAYERS_RU.md` и `NOTICE_DATA.txt`.