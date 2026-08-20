# Release checklist

1. Запустить полный production build на source-complete наборах.
2. Проверить `QUALITY_REPORT.json`, отсутствие `bad_residue` и `grammar_residue`.
3. Запустить `validate_stardict.py` и все локальные тесты.
4. Собрать словарь, builder и reader-пакеты в отдельные ZIP.
5. Посчитать SHA-256 и сохранить его в тексте GitHub Release.
6. Создать tag `vX.Y.Z` и опубликовать три архива как Release assets.
7. В описании Release указать источники, дату дампов, метрики QA и известные
   ограничения. Сами исходные дампы и пользовательские книги не загружать.
