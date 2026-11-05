"""Загрузка процессуальных кодексов: разбор страниц и выгрузка статей.

Проверяется не сеть, а то, что легко сломать незаметно: текст нормы
извлекается из контейнера документа, заголовок статьи отделяется от
текста, а служебные пометки о редакциях не попадают в норму.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.datasets.law_corpus import (
    LawArticle,
    articles_from_records,
    articles_to_records,
    build_chunks_from_articles,
)

ARTICLE_HTML = """
<html><body>
<nav><a href="/">Главная</a> Меню Реклама</nav>
<div class="document-page__content">
  <div class="document__style doc-style"><h1></h1>
    <p>ГПК РФ Статья 1. Законодательство о гражданском судопроизводстве</p>
  </div>
  <p class="no-indent"></p>
  <p>1. Порядок гражданского судопроизводства определяется
     <a href="/document/x/">Конституцией</a> Российской Федерации.</p>
  <p>2. Если международным договором установлены иные правила , применяются
     правила договора.</p>
  <div class="document__edit doc-edit"><p>(в ред. Федерального закона от 08.12.2020 N 428-ФЗ)</p></div>
  <div class="document__insert doc-insert"><p>(см. текст в предыдущей редакции)</p></div>
</div>
<div class="full-text">Рекламный блок: полный текст документа</div>
</body></html>
"""

TOC_HTML = """
<html><body>
<a href="/document/cons_doc_LAW_39570/aaa/">Раздел I. ОБЩИЕ ПОЛОЖЕНИЯ</a>
<a href="/document/cons_doc_LAW_39570/bbb/">Статья 1. Законодательство о судопроизводстве</a>
<a href="/document/cons_doc_LAW_39570/ccc/">Статья 2. Задачи судопроизводства</a>
<a href="/document/cons_doc_LAW_39570/ddd/#anchor">Статья 1. Законодательство о судопроизводстве</a>
<a href="/document/cons_doc_LAW_1/eee/">Статья 99. Чужой документ</a>
</body></html>
"""


class TestArticleParsing:
    def test_text_taken_from_document_container_only(self) -> None:
        from scripts.fetch_law_code import parse_article

        outcome = parse_article(ARTICLE_HTML, article="1")

        assert outcome.record is not None
        record = outcome.record
        assert record.title == "Законодательство о гражданском судопроизводстве"
        assert "Порядок гражданского судопроизводства" in record.text
        assert "Конституцией" in record.text
        # Рекламный блок и меню вне контейнера документа в норму не попадают.
        assert "Рекламный блок" not in record.text
        assert "Меню" not in record.text

    def test_editorial_notes_removed(self) -> None:
        """«(в ред. …)» — метаданные издания, а не содержание нормы."""
        from scripts.fetch_law_code import parse_article

        outcome = parse_article(ARTICLE_HTML, article="1")

        assert outcome.record is not None
        assert "в ред. Федерального закона" not in outcome.record.text
        assert "предыдущей редакции" not in outcome.record.text

    def test_link_spacing_normalised(self) -> None:
        from scripts.fetch_law_code import parse_article

        outcome = parse_article(ARTICLE_HTML, article="1")

        assert outcome.record is not None
        assert "правила, применяются" in outcome.record.text
        assert "правила , применяются" not in outcome.record.text

    def test_other_article_number_is_not_taken_as_heading(self) -> None:
        from scripts.fetch_law_code import parse_article

        outcome = parse_article(ARTICLE_HTML, article="7")

        assert outcome.record is not None
        assert outcome.record.title == ""

    def test_page_without_container_gives_nothing(self) -> None:
        from scripts.fetch_law_code import parse_article

        outcome = parse_article("<html><body>пусто</body></html>", article="1")

        assert outcome.record is None
        assert "контейнер" in outcome.reason

    def test_repealed_article_is_not_indexed(self) -> None:
        """«Утратила силу» — не норма: в индекс её пускать нельзя."""
        from scripts.fetch_law_code import parse_article

        html = """
        <html><body><div class="document-page__content">
          <p>ГПК РФ Статья 379. Утратила силу</p>
          <p>Статья 379. Утратила силу. - Федеральный закон от 04.12.2007 N 330-ФЗ.</p>
        </div></body></html>
        """

        outcome = parse_article(html, article="379")

        assert outcome.record is None
        assert outcome.repealed is True


class TestTableOfContents:
    def test_only_article_links_of_this_document(self) -> None:
        from scripts.fetch_law_code import collect_article_links

        links = collect_article_links(TOC_HTML, document="cons_doc_LAW_39570")

        assert set(links) == {"1", "2"}
        # Якорь внутри страницы — та же статья, второй раз не нужен.
        assert links["1"].endswith("/bbb/")


class TestArticleRecords:
    def test_records_round_trip(self) -> None:
        articles = [
            LawArticle(number="1", title="Начало", text="Текст первой статьи.", part=""),
            LawArticle(number="6.1", title="Срок", text="Текст статьи 6.1.", part=""),
        ]

        records = articles_to_records(articles, code="ГПК РФ")
        restored = articles_from_records(records)

        assert [article.number for article in restored] == ["1", "6.1"]
        assert restored[1].title == "Срок"
        assert restored[1].text == "Текст статьи 6.1."

    def test_chunks_keep_code_and_article(self) -> None:
        articles = [
            LawArticle(
                number="395",
                title="Ответственность за неисполнение денежного обязательства",
                text=(
                    "За пользование чужими денежными средствами вследствие их "
                    "неправомерного удержания подлежат уплате проценты на сумму "
                    "этих средств. Размер процентов определяется ключевой ставкой "
                    "Банка России, действовавшей в соответствующие периоды."
                ),
            )
        ]

        chunks = build_chunks_from_articles(articles, code="ГК РФ")

        assert chunks
        assert all(chunk.code == "ГК РФ" for chunk in chunks)
        assert all(chunk.article == "395" for chunk in chunks)
        assert chunks[0].chunk_id.startswith("ГК РФ:art395:")

    def test_jsonl_written_and_read_back(self, tmp_path: Path) -> None:
        from scripts.build_law_index import _from_jsonl

        path = tmp_path / "gpk.jsonl"
        articles = [
            LawArticle(
                number="1",
                title="Законодательство о гражданском судопроизводстве",
                text="Порядок гражданского судопроизводства определяется Конституцией.",
            )
        ]
        with path.open("w", encoding="utf-8") as handle:
            for record in articles_to_records(articles, code="ГПК РФ"):
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

        chunks, total = _from_jsonl([path], code="ГПК РФ")

        assert total > 0
        assert chunks
        assert chunks[0].code == "ГПК РФ"
