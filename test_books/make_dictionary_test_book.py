from __future__ import annotations

from html import escape
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape
import zipfile


OUT = Path(__file__).resolve().parent
TITLE = "RU Max Clean — тестовая книга словарей"
AUTHOR = "RU Max Clean QA"


CHAPTERS = [
    (
        "Как пользоваться книгой",
        [
            "Это оригинальный технический текст для проверки словарей на электронной читалке. Цитат из художественных произведений здесь нет.",
            "Скопируйте EPUB или FB2 на устройство, подключите RU-Max-Clean и companion-паки, затем открывайте слова долгим нажатием. Проверяйте сначала слово в обычном тексте, а затем ту же запись в контрольной строке.",
            "Рекомендуемый порядок: 1) основное русское ядро; 2) фразеология; 3) латинский слой; 4) французский литературный слой; 5) архаика; 6) сокращения; 7) литературные имена; 8) фэнтезийные термины.",
            "Если слой отключён, запись должна либо не находиться, либо находиться только в другом подключённом словаре. Это позволяет проверить границы пакетов и отсутствие случайных коллизий.",
            "На странице 10 есть контрольная полоса: для каждой группы попробуйте открыть несколько слов в разных формах, с прописной буквы, со строчной буквы и рядом со знаком препинания.",
        ],
    ),
    (
        "Русское ядро и формы слов",
        [
            "Вечером путник вошёл в тихий лес, поставил книгу на стол и посмотрел на дорогу. Обычные слова дом, вода, человек, ночь, город и письмо должны находиться в основном русском словаре.",
            "Проверьте формы: людьми, дорогой, говорили, смотрела, книгами, ночами. Если словарь поддерживает морфологические формы, карточка должна открываться не только для начальной формы.",
            "Отдельная проверка буквы ё: ёлка, елка, всё, все. Сравните результат для прописной и строчной буквы: Лес и лес, Река и река.",
            "Для проверки нормализации попробуйте слова впотьмах, непреходящий, скрупулёзный, скрупулезный, изощрённый, изощренный, предвещать, хитросплетение и щемящий. Они помещены в связный текст, а не в искусственный список.",
        ],
    ),
    (
        "Фразеологизмы",
        [
            "Старый редактор не любил бить баклуши и всегда доводил дело до конца. Когда возникала ахиллесова пята проекта, он не прятал проблему за пазухой.",
            "В споре герои выбирали между Сциллой и Харибдой, вспоминали нить Ариадны и надеялись, что труд не окажется сизифовым трудом. Иногда случалась пиррова победа или дело табак.",
            "Проверьте также дамоклов меч, танталовы муки, яблоко раздора, перейти Рубикон, лебединая песня, медвежья услуга, витать в облаках и на худой конец.",
            "Для фразового поиска попробуйте выделить всё выражение целиком, а затем отдельное слово из него. Хороший companion-пак должен объяснять устойчивый смысл, а не подменять его буквальным переводом.",
        ],
    ),
    (
        "Латинские выражения",
        [
            "В дневнике исследователя соседствовали латинские формулы: carpe diem, a priori, a posteriori, de facto, de jure, in medias res и status quo.",
            "Он пометил сомнительную запись как mea culpa, а чистый лист назвал tabula rasa. В отчёте встречались modus operandi, persona non grata, conditio sine qua non, vice versa и nota bene.",
            "Для расширенной проверки откройте ad hoc, ad infinitum, alma mater, alter ego, bona fide, ceteris paribus, et cetera, ex nihilo, in vitro, modus vivendi, non sequitur, pro forma и terra incognita.",
            "Сравните оригинальные написания с русскими вариантами: карпе дием, де-факто, де-юре, альтер эго, меа кульпа, статус-кво, табула раса и экслибрис.",
        ],
    ),
    (
        "Латинская лексика",
        [
            "Это отдельная проверка латинского словарного слоя, а не списка крылатых выражений: amicus, amor, aqua, bellum, causa, corpus, deus, gloria.",
            "Продолжение контрольной строки: homo, lingua, natura, pax, populus, terra, veritas и vita. Попробуйте открыть каждое слово отдельно и в предложении.",
            "Сравните короткие формы terra и vita с русским текстом вокруг них. Они должны открываться только при подключённом latin_wiktionary, не превращая основной русский словарь в смешанный.",
        ],
    ),
    (
        "Французская речь в русском тексте",
        [
            "В письме появились короткие реплики: bonjour, bonsoir, monsieur, madame, mademoiselle, merci, pardon и au revoir.",
            "Собеседник написал: chère amie, cher ami, à propos, tout à fait, s'il vous plaît и c'est-à-dire. Рядом стояли слова déjà, très, général, maréchal, comtesse и empereur.",
            "Проверьте русские транскрипции: бонжур, бонсуар, месье, мадам, мадемуазель, мерси, пардон, адьё, ма шер, шер ами, силь ву пле и сэ-та-дир.",
            "Особая проверка: m-lle, chère, très, voilà, Français и Française. Сравните вариант с диакритикой и без неё там, где обе формы есть в companion-паке.",
        ],
    ),
    (
        "Архаика и историческая лексика",
        [
            "В летописи воевода поднял десницу, склонил чело и коснулся перстом печати. На ланитах юноши блестели капли дождя, а старец поднял очи к небу.",
            "В старом городе стояли врата и погост; за заставой начинались волость и уезд. По улице ехали ямщики, рядом шли ратники и челядь.",
            "Проверьте отдельные карточки: доколе, аще, ибо, зело, оне, сударь, сударыня, кафтан, лапти, оброк, тягло, стряпчий, боярин, дьяк и опочивальня.",
            "Формы в этом разделе нужны для чтения классики и исторической прозы. Ожидайте пометы вроде «устар.», «истор.» или краткого современного эквивалента.",
        ],
    ),
    (
        "Старинная орфография и литературные термины",
        [
            "В рукописи сохранились формы Россія, Русскій, Санктъ-Петербургъ, Царьградъ и Новъгородъ. Такие записи проверяют исторический wiktionary-слой и его поддержку необычных букв.",
            "В театральной заметке встретились гамен, кавальеро, комтур, маэсе и фельдкурат. Это узкий literary_terms-пак: карточка должна объяснять термин и давать стилистическую помету.",
            "Проверьте также старые формы аббатъ, авва, абатство и Христосъ. Они нужны как контроль кириллической исторической графики, а не как рекомендация писать так в современном тексте.",
        ],
    ),
    (
        "Литературные имена и география",
        [
            "В условном читальном зале встретились Санчо Панса, Дон Кихот, Гэндальф, Воланд, Вальжан и Горио. Эти записи проверяют слой имён, а не обычный толковый словарь.",
            "Путешественник пересёк Гондор, заглянул в Земноморье и записал названия Изенгард, Андуин, Арагорн, Бильбо, Боромир и Галадриэль.",
            "Проверьте падежи: Гэндальфа, Гэндальфом, Мордором, Санчо Пансы, Кихота, Вальжана. Для имён особенно важны регистр и окончания.",
            "Если карточка имени содержит тип объекта, например «персонаж» или «географическое название», companion-пак работает правильно и не перегружает основное русское ядро.",
        ],
    ),
    (
        "Фэнтезийные термины",
        [
            "На карте были отмечены Шир и Рохан; рядом стояли хоббиты, орки, энты и назгулы. В башне хранился палантир, а в песне упоминались майары и истари.",
            "Проверьте формы: хоббитов, орками, энтов, назгулов, палантире, Роханом, Широм, кольцом Всевластия и истинным именем.",
            "В этом разделе слова намеренно находятся в обычных предложениях. Сравните чародей, маг, дракон, тень и равновесие с более узкими терминами из fantasy-пака.",
        ],
    ),
    (
        "Литературные сокращения",
        [
            "В примечании редактор написал: г-н Орлов, г-жа Орлова, стр 14, примеч 2, ср раздел 3, фр перевод, изд 1890 и др сведения.",
            "Проверьте также полные формы рядом с сокращениями: господин, госпожа, страницы, примечание, сравни, французский, издание и другие.",
            "Попробуйте открыть сокращение с точкой и без точки: стр. и стр, примеч. и примеч, др. и др. На разных читалках пунктуация может входить в выделение по-разному.",
        ],
    ),
    (
        "Пограничные и технические случаи",
        [
            "Проверьте выделение рядом с кавычками: «status quo», (carpe diem), [десница], «Гэндальф» и “monsieur”. Затем повторите поиск в начале и в конце строки.",
            "Проверьте дефисы и апострофы: де-факто, экслибрис, ma chère, c'est-à-dire, s'il vous plaît, кольцо Всевластия и Санчо-Панса.",
            "Сравните словарные окна для заглавной и строчной буквы, для формы единственного и множественного числа, а также для текста с буквой ё. Отдельно убедитесь, что короткое de не перехватывает русскую часть соседнего слова.",
            "Если на устройстве можно выбирать источник словаря, зафиксируйте, какой слой открыл карточку. Это поможет отличить правильное покрытие от случайного совпадения.",
        ],
    ),
    (
        "Контрольная полоса",
        [
            "Ядро: человек; дорогой; говорили; скрупулёзный; предвещать.",
            "Фразеология: бить баклуши; ахиллесова пята; нить Ариадны; перейти Рубикон; держать камень за пазухой.",
            "Latin: carpe diem; ad hoc; status quo; mea culpa; tabula rasa; persona non grata.",
            "Latin words: amicus; aqua; corpus; homo; lingua; natura; veritas; vita.",
            "Français: monsieur; madame; chère; très; déjà; au revoir; месье; мадам; мерси.",
            "Архаика: десница; перст; ланиты; очи; доколе; стряпчий; челядь.",
            "Историческая графика: Россія; Русскій; Санктъ-Петербургъ; Царьградъ; фельдкурат.",
            "Имена: Санчо Панса; Гэндальф; Воланд; Вальжан; Мордор; Земноморье.",
            "Fantasy: хоббиты; орки; энты; назгулы; палантир; Рохан; Шир; кольцо Всевластия.",
            "Сокращения: г-н; г-жа; стр; примеч; ср; фр; изд; др.",
        ],
    ),
]


def xhtml_body() -> str:
    parts = []
    for index, (title, paragraphs) in enumerate(CHAPTERS, start=1):
        parts.append(f'<section id="ch{index}"><h2>{escape(title)}</h2>')
        for paragraph in paragraphs:
            parts.append(f'<p>{escape(paragraph)}</p>')
        parts.append('</section>')
    return '\n'.join(parts)


def make_xhtml() -> str:
    body = xhtml_body()
    return f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="ru" xml:lang="ru">
<head><title>{escape(TITLE)}</title><link rel="stylesheet" type="text/css" href="style.css" /></head>
<body><h1>{escape(TITLE)}</h1><p class="meta">Автор: {escape(AUTHOR)}. Техническая книга для проверки словарей.</p>{body}</body>
</html>
'''


def make_nav() -> str:
    items = ''.join(
        f'<li><a href="test.xhtml#ch{index}">{escape(title)}</a></li>'
        for index, (title, _) in enumerate(CHAPTERS, start=1)
    )
    return f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="ru">
<head><title>Оглавление</title></head><body><nav epub:type="toc" id="toc"><h1>Оглавление</h1><ol>{items}</ol></nav></body></html>
'''


def make_opf() -> str:
    return f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">urn:uuid:ru-max-clean-dictionary-test-1</dc:identifier>
    <dc:title>{escape(TITLE)}</dc:title><dc:language>ru</dc:language><dc:creator>{escape(AUTHOR)}</dc:creator>
    <dc:description>Оригинальная техническая книга для проверки словарей на электронной читалке.</dc:description>
    <meta property="dcterms:modified">2026-08-20T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="book" href="test.xhtml" media-type="application/xhtml+xml" properties="scripted" />
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav" />
    <item id="css" href="style.css" media-type="text/css" />
  </manifest>
  <spine><itemref idref="book" /></spine>
</package>
'''


def make_container() -> str:
    return '''<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml" /></rootfiles>
</container>
'''


def make_fb2() -> str:
    def p(text: str) -> str:
        return f'<p>{xml_escape(text)}</p>'

    sections = []
    for title, paragraphs in CHAPTERS:
        sections.append('<section><title><p>%s</p></title>%s</section>' % (
            xml_escape(title), ''.join(p(text) for text in paragraphs)
        ))
    return '''<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">
  <description><title-info><genre>nonfiction</genre><author><first-name>RU Max Clean</first-name><last-name>QA</last-name></author>
  <book-title>%s</book-title><lang>ru</lang></title-info><document-info><program-used>RU Max Clean test book generator</program-used><date value="2026-08-20">20 августа 2026</date></document-info>
  <publish-info><publisher>RU Max Clean</publisher></publish-info></description>
  <body><section><title><p>%s</p></title><p>%s</p>%s</section></body>
</FictionBook>
''' % (
        xml_escape(TITLE), xml_escape(TITLE),
        xml_escape('Оригинальная техническая книга для проверки словарей на электронной читалке.'),
        ''.join(sections),
    )


def write_epub(path: Path) -> None:
    with zipfile.ZipFile(path, 'w') as book:
        info = zipfile.ZipInfo('mimetype')
        info.compress_type = zipfile.ZIP_STORED
        info.date_time = (2020, 1, 1, 0, 0, 0)
        book.writestr(info, 'application/epub+zip')
        files = {
            'META-INF/container.xml': make_container(),
            'OEBPS/content.opf': make_opf(),
            'OEBPS/nav.xhtml': make_nav(),
            'OEBPS/test.xhtml': make_xhtml(),
            'OEBPS/style.css': '''body { font-family: serif; line-height: 1.45; margin: 5%; } h1 { text-align: center; } h2 { margin-top: 2em; border-bottom: 1px solid #888; } p { text-align: left; } .meta { text-align: center; font-style: italic; }''',
        }
        for name, data in files.items():
            book.writestr(name, data.encode('utf-8'), compress_type=zipfile.ZIP_DEFLATED)


def main() -> None:
    epub = OUT / 'RU-Max-Clean-Dictionary-Test-Book.epub'
    fb2 = OUT / 'RU-Max-Clean-Dictionary-Test-Book.fb2'
    readme = OUT / 'README_RU.txt'
    write_epub(epub)
    fb2.write_text(make_fb2(), encoding='utf-8', newline='\n')
    readme.write_text(
        'RU Max Clean — тестовая книга словарей\n\n'
        'Файлы: EPUB для большинства современных читалок и FB2 для KOReader/FBReader.\n'
        'Книга полностью оригинальная и содержит контрольные слова по всем текущим слоям.\n\n'
        'Порядок проверки: подключите ядро, затем включайте companion-паки по одному и открывайте слова долгим нажатием.\n'
        'Проверьте обычную и изменённую форму, регистр, букву ё, дефисы, диакритику, кавычки и многословные выражения.\n'
        'Раздел «Контрольная полоса» содержит короткий итоговый список.\n\n'
        'Книга намеренно не входит в builder-релизы GitHub: это локальный QA-инструмент.\n',
        encoding='utf-8', newline='\n'
    )
    print(epub)
    print(fb2)
    print(readme)


if __name__ == '__main__':
    main()
