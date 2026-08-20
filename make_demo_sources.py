#!/usr/bin/env python3
import bz2
import gzip
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

records = [
    {"word":"ключ","lang":"Русский","lang_code":"ru","pos":"noun","senses":[
        {"glosses":["Предмет для отпирания и запирания замка."]},
        {"glosses":["Источник воды, выходящий из земли."]},
        {"glosses":["Средство или способ, позволяющий понять или решить что-либо."]},
        {"glosses":["Знак, определяющий высоту нот в музыкальной записи."]}],
     "forms":[{"form":"ключами","tags":["ins","pl"]}]},
    {"word":"полупроводниковый","lang":"Русский","lang_code":"ru","pos":"adj","senses":[
        {"glosses":["Относящийся к полупроводникам или основанный на их свойствах."]}],
     "forms":[{"form":"полупроводниковому","tags":["dat","sg"]},{"form":"полупроводниковыми","tags":["ins","pl"]}]},
    {"word":"сталь","lang":"Русский","lang_code":"ru","pos":"noun","senses":[
        {"glosses":["Сплав железа с углеродом и другими элементами, обладающий высокой прочностью."]}],
     "forms":[{"form":"стали","tags":["gen","sg"]}]},
    {"word":"стать","lang":"Русский","lang_code":"ru","pos":"verb","senses":[
        {"glosses":["Начать находиться в каком-либо состоянии или приобрести какое-либо качество."]}],
     "forms":[{"form":"стали","tags":["past","pl"]}]},
    {"word":"ёлка","lang":"Русский","lang_code":"ru","pos":"noun","senses":[
        {"glosses":["Ель, а также украшенное хвойное дерево, устанавливаемое к зимнему празднику."]}]},
    {"word":"рѣчь","lang":"Русский (дореформенная орфография)","lang_code":"ru-old","pos":"noun","senses":[
        {"glosses":["Способность говорить; словесное выражение мыслей."]}]},
    {"word":"pH","lang":"Русский","lang_code":"ru","pos":"noun","senses":[
        {"glosses":["Водородный показатель, характеризующий кислотность или щёлочность раствора."]}]},
    {"word":"ключами","lang":"Русский","lang_code":"ru","pos":"noun","senses":[
        {"form_of":[{"word":"ключ"}],"glosses":["творительный падеж множественного числа слова ключ"],"tags":["form-of"]}]},
    {"word":"-у","lang":"Русский","lang_code":"ru","pos":"suffix","senses":[
        {"glosses":["Окончание дательного падежа некоторых существительных."]}]},
    {"word":"датив","lang":"Русский","lang_code":"ru","pos":"noun","senses":[
        {"glosses":["Дательный падеж."]}]},
    {"word":"вкопать","lang":"Русский","lang_code":"ru","pos":"verb","senses":[
        {"glosses":["Поместить и укрепить внутри вырытого углубления."]}]},
    {"word":"вкопанный","lang":"Русский","lang_code":"ru","pos":"adj","senses":[
        {"glosses":["Страд. прич. прош. вр. от вкопать"]}]},
    {"word":"поместить","lang":"Русский","lang_code":"ru","pos":"verb","senses":[
        {"glosses":["Расположить что-либо в определённом месте."]}],
     "forms":[{"form":"помещённый","tags":["past","passive","participle"]}]},
    {"word":"укрепить","lang":"Русский","lang_code":"ru","pos":"verb","senses":[
        {"glosses":["Сделать более прочным или устойчивым."]}],
     "forms":[{"form":"укреплённый","tags":["past","passive","participle"]}]},
    {"word":"сервитут","lang":"Русский","lang_code":"ru","pos":"noun","senses":[
        {"glosses":["(в земельных отношениях) ограниченное право пользования чужой вещью."]}]},
    {"word":"гистерезис","lang":"Русский","lang_code":"ru","pos":"noun","senses":[
        {"glosses":["Свойство систем, не сразу реагирующих на приложенные воздействия."]}]},
    {"word":"осветить","lang":"Русский","lang_code":"ru","pos":"verb","senses":[
        {"glosses":["Сделать светлым, направив свет на что-либо."]}]},
    {"word":"освещённый","lang":"Русский","lang_code":"ru","pos":"adj","senses":[
        {"glosses":["Страд. прич. прош. вр. от осветить"]},
        {"glosses":["Адъектив. такой, где есть освещение"]}],
     "forms":[{"form":"освещенному","tags":["dat","sg"]}]},
]
raw_jsonl = "".join(json.dumps(obj, ensure_ascii=False) + "\n" for obj in records).encode("utf-8")
# Keep the tracked fixture byte-for-byte stable across test runs.  The previous
# gzip.open() call embedded the current wall-clock time in the header, making a
# clean checkout appear dirty after every demo build.
(ROOT / "sample_raw.jsonl.gz").write_bytes(gzip.compress(raw_jsonl, mtime=0))

lexemes = [
    {
        "type": "lexeme", "id": "L1", "language": "Q7737",
        "lemmas": {"ru": {"language": "ru", "value": "эвтектика"}},
        "senses": [{"id": "L1-S1", "glosses": {"ru": {"language": "ru", "value": "Смесь веществ, имеющая наименьшую температуру плавления среди смесей данного состава."}}}],
        "forms": [
            {"id": "L1-F1", "representations": {"ru": {"language": "ru", "value": "эвтектики"}}},
            {"id": "L1-F2", "representations": {"ru": {"language": "ru", "value": "эвтектикой"}}},
        ],
    },
    {
        "type": "lexeme", "id": "L2", "language": "Q7737",
        "lemmas": {"ru": {"language": "ru", "value": "ключ"}},
        "senses": [{"id": "L2-S1", "glosses": {"ru": {"language": "ru", "value": "Резервное значение, которое не должно добавляться поверх Викисловаря."}}}],
        "forms": [{"id": "L2-F1", "representations": {"ru": {"language": "ru", "value": "ключиком"}}}],
    },
    {
        "type": "lexeme", "id": "L3", "language": "Q1860",
        "lemmas": {"en": {"language": "en", "value": "engineering"}},
        "senses": [], "forms": [],
    },
]
lexeme_payload = "[\n" + ",\n".join(json.dumps(x, ensure_ascii=False) for x in lexemes) + "\n]\n"
(ROOT / "sample_wikidata_lexemes.json.bz2").write_bytes(bz2.compress(lexeme_payload.encode("utf-8")))

wikipedia_xml = """<?xml version="1.0" encoding="utf-8"?>
<mediawiki>
  <page>
    <title>Криогенная техника</title><ns>0</ns>
    <revision><text>'''Криогенная техника''' — совокупность методов и устройств для получения и использования очень низких температур. Применяется в науке и промышленности.

[[Категория:Криогенная техника]]
[[Категория:Инженерные дисциплины]]</text></revision>
  </page>
  <page>
    <title>Шунт (электротехника)</title><ns>0</ns>
    <revision><text>'''Шунт''' — электрический проводник, подключаемый параллельно участку электрической цепи для отвода части тока.

[[Категория:Электротехника]]</text></revision>
  </page>
  <page>
    <title>Ключ (информатика)</title><ns>0</ns>
    <revision><text>'''Ключ''' — специальный идентификатор в структуре данных.

[[Категория:Информатика]]</text></revision>
  </page>
  <page>
    <title>Гистерезис</title><ns>0</ns>
    <revision><text>'''Гистерезис''' — зависимость состояния системы не только от текущего воздействия, но и от истории предшествующих воздействий.

[[Категория:Физика]]</text></revision>
  </page>
  <page>
    <title>Криогеника</title><ns>0</ns><redirect title="Криогенная техника" />
  </page>
  <page>
    <title>Иванов, Иван Иванович</title><ns>0</ns>
    <revision><text>'''Иван Иванов''' — российский физик, родился 1 января 1900 года.

[[Категория:Физики России]]</text></revision>
  </page>
</mediawiki>""".encode("utf-8")
(ROOT / "sample_ruwiki.xml.bz2").write_bytes(bz2.compress(wikipedia_xml))
