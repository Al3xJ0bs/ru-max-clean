#!/usr/bin/env python3
"""Human-readable console summaries for RU Max Clean.

Raw counters are kept in BUILD_STATS.json; the console is deliberately concise and
uses Russian labels so a long build can be understood without reading JSON blobs.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Mapping, Any

WIDTH = 68


def fmt_int(value: Any) -> str:
    try:
        return f"{int(value):,}".replace(",", " ")
    except Exception:
        return str(value)


def fmt_float(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return str(value)


def fmt_bytes(value: Any) -> str:
    try:
        n = float(value)
    except Exception:
        return str(value)
    units = ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ")
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    if i == 0:
        return f"{int(n)} {units[i]}"
    return f"{n:.2f} {units[i]}"


def yes_no(value: Any) -> str:
    return "да" if bool(value) else "нет"


def section(title: str, rows: Iterable[tuple[str, Any]], *, subtitle: str | None = None) -> None:
    rows = list(rows)
    print()
    print("=" * WIDTH)
    print(f" {title}")
    if subtitle:
        print(f" {subtitle}")
    print("=" * WIDTH)
    if not rows:
        print("  Нет данных")
        return
    label_width = min(42, max(len(label) for label, _ in rows))
    for label, value in rows:
        print(f"  {label:<{label_width}}  {value}")


def source_wiktionary(stats: Mapping[str, Any]) -> None:
    section("ВИКИСЛОВАРЬ / KAIKKI", [
        ("Обработано записей", fmt_int(stats.get("processed", 0))),
        ("Записей целевых языков", fmt_int(stats.get("accepted_language_records", 0))),
        ("Добавлено определений", fmt_int(stats.get("definitions_added", 0))),
        ("Форм и вариантов связано", fmt_int(stats.get("form_or_alt_links_seen", 0))),
        ("Перенаправлений найдено", fmt_int(stats.get("redirects_seen", 0))),
    ])


def source_opencorpora(stats: Mapping[str, Any]) -> None:
    section("OPENCORPORA / МОРФОЛОГИЯ", [
        ("Лемм обработано", fmt_int(stats.get("lemmas_seen", 0))),
        ("Словоформ связано", fmt_int(stats.get("forms_seen", 0))),
    ])


def source_wikidata(stats: Mapping[str, Any]) -> None:
    section("WIKIDATA LEXEMES", [
        ("Сущностей обработано", fmt_int(stats.get("entities_processed", 0))),
        ("Русских лексем", fmt_int(stats.get("russian_lexemes", 0))),
        ("Добавлено русских определений", fmt_int(stats.get("glosses_added", 0))),
        ("Словоформ связано", fmt_int(stats.get("forms_linked", 0))),
        ("Уже имелись в основной базе", fmt_int(stats.get("skipped_existing_lemmas", 0))),
        ("Без русского определения", fmt_int(stats.get("without_russian_gloss", 0))),
    ])


def source_dal(stats: Mapping[str, Any]) -> None:
    section("СЛОВАРЬ ДАЛЯ", [
        ("Исходных статей", fmt_int(stats.get("source_entries", 0))),
        ("Добавлено новых определений", fmt_int(stats.get("definitions_added", 0))),
        ("Пропущено: уже определены", fmt_int(stats.get("skipped_existing", 0))),
        ("Отбраковано", fmt_int(stats.get("rejected", 0))),
        ("Добавлено синонимов", fmt_int(stats.get("synonyms_added", 0))),
    ])


def source_wikipedia(stats: Mapping[str, Any]) -> None:
    rows = [
        ("Страниц в дампе обработано", fmt_int(stats.get("pages_processed", 0))),
        ("Страниц основного пространства", fmt_int(stats.get("namespace0_pages", 0))),
        ("Кандидатов по проф. тематикам", fmt_int(stats.get("domain_candidates", 0))),
        ("Добавлено определений", fmt_int(stats.get("definitions_added", 0))),
        ("Уже имелись в основной базе", fmt_int(stats.get("skipped_existing", 0))),
        ("Улучшено слабых определений", fmt_int(stats.get("quality_upgrades", 0))),
        ("Отклонено / не является определением", fmt_int(stats.get("rejected_or_nondefinitional", 0))),
    ]
    if "redirect_aliases_seen" in stats:
        rows.append(("Перенаправлений/алиасов просмотрено", fmt_int(stats.get("redirect_aliases_seen", 0))))
    if stats.get("prepared_cache_reused"):
        rows.append(("Подготовленный кэш Wikipedia", "использован"))
        rows.append(("Кандидатов в подготовленном кэше", fmt_int(stats.get("prepared_candidates", 0))))
    if stats.get("worker_processes"):
        rows.append(("Процессов очистки Wikipedia", fmt_int(stats.get("worker_processes", 0))))
    section("WIKIPEDIA / ПРОФЕССИОНАЛЬНАЯ ТЕРМИНОЛОГИЯ", rows)



def semantic_cleanup(stats: Mapping[str, Any]) -> None:
    section("АКТИВНАЯ СЕМАНТИЧЕСКАЯ ОЧИСТКА", [
        ("Определений в базе", fmt_int(stats.get("definitions_total", stats.get("definitions_examined", 0)))),
        ("Кандидатов реально проверено", fmt_int(stats.get("definitions_examined", 0))),
        ("Процессов очистки", fmt_int(stats.get("worker_processes", 1))),
        ("Нормализовано заголовков Даля", fmt_int(stats.get("dal_headwords_normalized", 0))),
        ("Удалено дубликатов Даля после нормализации", fmt_int(stats.get("dal_fallback_conflicts_removed", 0))),
        ("Переписано определений", fmt_int(stats.get("definitions_rewritten", 0))),
        ("Удалено не-определений", fmt_int(stats.get("definitions_removed", 0))),
        ("Текстовых ссылок превращено в алиасы", fmt_int(stats.get("textual_aliases_converted", 0))),
        ("Кратких «О ...» превращено в алиасы", fmt_int(stats.get("about_aliases_converted", 0))),
        ("Канонических целей алиасов связано", fmt_int(stats.get("alias_targets_linked", 0))),
        ("Ссылок переписано в самостоятельный смысл", fmt_int(stats.get("alias_fallback_definitions", 0))),
        ("Дублей «аналогично русскому слову» удалено", fmt_int(stats.get("old_equivalence_duplicates_removed", 0))),
        ("Заглушек старых языков сохранено", fmt_int(stats.get("old_equivalence_retained", 0))),
        ("Удалено хвостов с примерами", fmt_int(stats.get("example_tails_removed", 0))),
        ("Удалено корпусных/источниковых хвостов", fmt_int(stats.get("corpus_tails_removed", 0))),
        ("Удалено ссылок-цитат", fmt_int(stats.get("citations_removed", 0))),
        ("Удалено внутренних номеров значений [1]/[2]", fmt_int(stats.get("sense_references_removed", 0))),
        ("Удалено URL", fmt_int(stats.get("urls_removed", 0))),
        ("Убрано начальных метаданных", fmt_int(stats.get("leading_metadata_removed", 0))),
        ("Скобочный контекст переписан в смысл", fmt_int(stats.get("leading_context_rewritten", 0))),
        ("Раскрыто сокращений", fmt_int(stats.get("abbreviations_expanded", 0))),
        ("Убрано повторов заголовка", fmt_int(stats.get("headword_prefixes_removed", 0))),
        ("Хвостовых пояснений превращено в смысл", fmt_int(stats.get("trailing_descriptors_rewritten", 0))),
        ("Убрано пустых «Это ...»", fmt_int(stats.get("empty_copulas_removed", 0))),
        ("Обороты «О том, кто ...» переписано", fmt_int(stats.get("about_fragments_rewritten", 0))),
        ("Обрезано склеенных статей Даля", fmt_int(stats.get("dal_clusters_trimmed", 0))),
        ("Сжато длинных статей Даля до ядра", fmt_int(stats.get("dal_long_compacted", 0))),
        ("Удалено энциклопедического шума Wikipedia", fmt_int(stats.get("wikipedia_entity_noise_removed", 0))),
        ("Убрано лишних предложений Wikipedia", fmt_int(stats.get("wikipedia_extra_sentences_removed", 0))),
        ("Убрано датированных исторических хвостов", fmt_int(stats.get("wikipedia_history_tails_removed", 0))),
        ("Именованных Wikipedia-объектов сжато до класса", fmt_int(stats.get("wikipedia_named_cores_compacted", 0))),
        ("Исправлено сломанных хвостов Wikipedia", fmt_int(stats.get("wikipedia_broken_tails_removed", 0))),
        ("Удалено сломанных фрагментов Wikipedia", fmt_int(stats.get("wikipedia_broken_fragments_removed", 0))),
        ("Удалено списков/дизамбигов Wikipedia", fmt_int(stats.get("wikipedia_list_residue_removed", 0))),
        ("Кандидатов на точечное улучшение", fmt_int(stats.get("wikipedia_rescue_targets", 0))),
        ("Совпадений в Wikipedia-кэше", fmt_int(stats.get("wikipedia_rescue_matching_candidates", 0))),
        ("Восстановлено значений из Wikipedia", fmt_int(stats.get("wikipedia_rescue_missing_definitions_rescued", 0))),
        ("Улучшено слабых значений из Wikipedia", fmt_int(stats.get("wikipedia_rescue_weak_definitions_upgraded", 0))),
        ("Отклонено rescue-кандидатов", fmt_int(stats.get("wikipedia_rescue_rejected_candidates", 0))),
        ("Схлопнуто дублей", fmt_int(stats.get("duplicates_collapsed", 0))),
    ])


def resolve(stats: Mapping[str, Any]) -> None:
    section("РАЗРЕШЕНИЕ ССЫЛОК И СЛОВОФОРМ", [
        ("Связей распространено по цепочкам", fmt_int(stats.get("propagated", 0))),
        ("Неразрешимых ссылок удалено", fmt_int(stats.get("unresolved_removed", 0))),
    ])


def form_quality(stats: Mapping[str, Any]) -> None:
    section("ОТОБРАЖЕНИЕ СЛОВОФОРМ", [
        ("Подсказок рассмотрено", fmt_int(stats.get("hints_considered", 0))),
        ("Создано естественных определений", fmt_int(stats.get("display_overrides_generated", 0))),
        ("Пропущено: есть собственное значение", fmt_int(stats.get("skipped_direct_lexical_sense", 0))),
        ("Пропущено: неоднозначная основа", fmt_int(stats.get("skipped_ambiguous_target", 0))),
    ])


def quality(stats: Mapping[str, Any]) -> None:
    rows = [
        ("Проверено определений", fmt_int(stats.get("definitions_audited", 0))),
        ("Средний эвристический балл", f"{fmt_float(stats.get('average_quality_score', 0))} / 100"),
        ("Естественных определений словоформ", fmt_int(stats.get("display_overrides", 0))),
        ("Кандидатов в QUALITY_REVIEW.tsv", fmt_int(stats.get("review_rows", 0))),
    ]
    buckets = stats.get("score_buckets") or {}
    if isinstance(buckets, Mapping):
        rows.extend([
            ("Качество 85-100", fmt_int(buckets.get("85-100", 0))),
            ("Качество 70-84", fmt_int(buckets.get("70-84", 0))),
            ("Качество 50-69", fmt_int(buckets.get("50-69", 0))),
            ("Качество 0-49", fmt_int(buckets.get("0-49", 0))),
        ])
    info = stats.get("informational_counts") or {}
    if isinstance(info, Mapping) and info.get("onomastic_stub"):
        rows.append(("Кратких имён/фамилий/топонимов", fmt_int(info.get("onomastic_stub", 0))))
    if isinstance(info, Mapping) and info.get("concise_gloss"):
        rows.append(("Корректных кратких значений", fmt_int(info.get("concise_gloss", 0))))
    if stats.get("onomastic_review_rows") is not None:
        rows.append(("Строк в QUALITY_ONOMASTICS.tsv", fmt_int(stats.get("onomastic_review_rows", 0))))
    if stats.get("concise_review_rows") is not None:
        rows.append(("Строк в QUALITY_CONCISE.tsv", fmt_int(stats.get("concise_review_rows", 0))))
    section("КОНТРОЛЬ КАЧЕСТВА", rows)
    warnings = stats.get("warning_counts") or {}
    if isinstance(warnings, Mapping) and warnings:
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
        }
        ordered = sorted(warnings.items(), key=lambda kv: (-int(kv[1]), str(kv[0])))
        print("  Предупреждения контроля качества:")
        for key, value in ordered:
            label = warning_labels.get(str(key), str(key))
            print(f"    - {label:<36} {fmt_int(value)}")


def database_totals(lemmas: Any, definitions: Any, lookup_keys: Any) -> None:
    section("ИТОГ СЛОВАРНОЙ БАЗЫ", [
        ("Уникальных лемм", fmt_int(lemmas)),
        ("Определений", fmt_int(definitions)),
        ("Поисковых ключей до экспорта", fmt_int(lookup_keys)),
    ])


def stardict(result: Mapping[str, Any]) -> None:
    section("ГОТОВЫЙ STARDICT", [
        ("Поисковых ключей", fmt_int(result.get("wordcount", 0))),
        ("Канонических статей", fmt_int(result.get("canonical_articles", 0))),
        ("Однозначных ключей", fmt_int(result.get("single_target_keys", 0))),
        ("Неоднозначных ключей", fmt_int(result.get("ambiguous_keys", 0))),
        ("Наборов неоднозначных статей", fmt_int(result.get("ambiguous_article_sets", 0))),
        ("Переопределений словоформ", fmt_int(result.get("display_overrides", 0))),
        ("Уникальных текстов словоформ", fmt_int(result.get("unique_override_bodies", 0))),
        ("Размер .dict", fmt_bytes(result.get("dict_bytes", 0))),
        ("Размер .idx", fmt_bytes(result.get("idx_bytes", 0))),
    ])
    files = result.get("files") or []
    if files:
        print("  Файлы:")
        for path in files:
            print(f"    - {path}")


def turbo(stats: Mapping[str, Any]) -> None:
    rows = [
        ("orjson", "включён" if stats.get("orjson") else "не установлен / fallback"),
        ("lxml", "включён" if stats.get("lxml") else "не установлен / fallback"),
        ("rapidgzip", "включён (параллельный gzip)" if stats.get("rapidgzip") else "не установлен / stdlib gzip"),
        ("indexed_bzip2", "включён (параллельный bzip2)" if stats.get("indexed_bzip2") else "не установлен / stdlib bz2"),
        ("Потоков CPU", fmt_int(stats.get("cpus", 0))),
        ("SQLite threads (сортировка/индексы)", fmt_int(stats.get("workers", 0))),
        ("ОЗУ", f"{fmt_int(stats.get('ram_mib', 0))} МиБ"),
        ("SQLite cache", f"{fmt_int(stats.get('cache_mib', 0))} МиБ"),
        ("SQLite mmap", f"{fmt_int(stats.get('mmap_mib', 0))} МиБ"),
    ]
    insert_at = 4
    if stats.get("rapidgzip"):
        rows.insert(insert_at, ("Потоков rapidgzip", fmt_int(stats.get("gzip_threads", 1))))
        insert_at += 1
    if stats.get("indexed_bzip2"):
        rows.insert(insert_at, ("Потоков indexed_bzip2", fmt_int(stats.get("bzip2_threads", 1))))
    section("УСКОРЕНИЕ И РЕСУРСЫ", rows)


def added_source(title: str, count: Any, *, path: Any | None = None) -> None:
    rows = [("Добавлено определений", fmt_int(count))]
    if path is not None:
        rows.append(("Источник", str(path)))
    section(title, rows)


def validation(stats: Mapping[str, Any]) -> None:
    section("ПРОВЕРКА STARDICT — УСПЕШНО", [
        ("Поисковых ключей", fmt_int(stats.get("wordcount", 0))),
        ("Уникальных указателей на статьи", fmt_int(stats.get("unique_article_pointers", 0))),
        ("Размер .idx", fmt_bytes(stats.get("idx_bytes", 0))),
        ("Размер .dict", fmt_bytes(stats.get("dict_bytes", 0))),
    ])


def fmt_duration(seconds: Any) -> str:
    try:
        s = float(seconds)
    except Exception:
        return str(seconds)
    if s < 60:
        return f"{s:.1f} с"
    m, sec = divmod(int(round(s)), 60)
    if m < 60:
        return f"{m} мин {sec:02d} с"
    h, m = divmod(m, 60)
    return f"{h} ч {m:02d} мин"


def performance(stats: Mapping[str, Any]) -> None:
    stages = stats.get("stages") or []
    rows: list[tuple[str, Any]] = []
    if isinstance(stages, list):
        # Show slowest stages first; tiny source HEAD checks are omitted from the
        # console but remain in BUILD_STATS.json.
        significant = []
        for item in stages:
            if not isinstance(item, Mapping):
                continue
            sec = float(item.get("seconds", 0) or 0)
            if sec < 0.5:
                continue
            significant.append(item)
        significant.sort(key=lambda x: float(x.get("seconds", 0) or 0), reverse=True)
        for item in significant[:12]:
            name = str(item.get("stage", "Этап"))
            suffix = " [кэш]" if item.get("cached") else ""
            rows.append((name + suffix, fmt_duration(item.get("seconds", 0))))
    rows.append(("Общее время", fmt_duration(stats.get("total_seconds", 0))))
    restored = stats.get("restored_stage")
    if restored:
        rows.append(("Самый глубокий использованный кэш", str(restored)))
    section("ПРОИЗВОДИТЕЛЬНОСТЬ СБОРКИ", rows)
