"""Разбор кодекса: статьи, части, нарезка на пассажи."""

from __future__ import annotations

from app.datasets.law_corpus import (
    LawArticle,
    build_chunks,
    chunk_article,
    normalize_law_text,
    parse_articles,
)

TEXT = """
Часть первая

Статья 1. Основные начала гражданского законодательства
1. Гражданское законодательство основывается на признании равенства
участников регулируемых им отношений, неприкосновенности собственности.

Статья 2. Отношения, регулируемые гражданским законодательством
1. Гражданское законодательство определяет правовое положение
участников гражданского оборота.

Часть вторая

Статья 454. Договор купли-продажи
1. По договору купли-продажи одна сторона обязуется передать вещь.
"""


class TestParsing:
    def test_article_count(self) -> None:
        articles = parse_articles(TEXT)
        assert [a.number for a in articles] == ["1", "2", "454"]

    def test_titles(self) -> None:
        articles = parse_articles(TEXT)
        assert articles[0].title.startswith("Основные начала")
        assert articles[2].title == "Договор купли-продажи"

    def test_parts(self) -> None:
        articles = parse_articles(TEXT)
        assert articles[0].part == "Часть первая"
        assert articles[2].part == "Часть вторая"

    def test_body_does_not_include_next_article(self) -> None:
        articles = parse_articles(TEXT)
        assert "Статья 2" not in articles[0].text
        assert "купли-продажи" not in articles[1].text

    def test_normalize_joins_line_breaks(self) -> None:
        text = normalize_law_text("участников\nотношений")
        assert text == "участников отношений"

    def test_empty_text(self) -> None:
        assert parse_articles("") == []


class TestPageFurniture:
    """Колонтитул PDF-издания не должен попадать в текст нормы."""

    def test_footer_removed(self) -> None:
        text = (
            "Статья 1. Название\n"
            "1. Первое предложение нормы.\n"
            "Дата актуализации: 25.05.2026\n"
            "Информация предоставлена для личного использования\n"
            "WWW.GARANT.RU\n"
            "© ООО \"Гарант\", 2026.\n"
            "Компания «Гарант» работает с 1990г.\n"
            "2. Второе предложение нормы.\n"
        )
        articles = parse_articles(text)
        assert len(articles) == 1
        body = articles[0].text
        assert "GARANT" not in body
        assert "Дата обновления" not in body
        assert "Первое предложение" in body
        assert "Второе предложение" in body


class TestChunking:
    def test_long_article_splits(self) -> None:
        article = LawArticle(
            number="395",
            title="Ответственность за неисполнение денежного обязательства",
            text=" ".join(f"Предложение номер {index} о сроках и процентах." for index in range(60)),
            part="Часть первая",
        )
        chunks = chunk_article(article, max_chars=400, overlap=50)
        assert len(chunks) > 1
        assert all(chunk.article == "395" for chunk in chunks)
        assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)

    def test_chunk_id_is_stable(self) -> None:
        article = LawArticle(number="395", title="Название", text="Короткий текст нормы. " * 20)
        first = chunk_article(article)[0].chunk_id
        second = chunk_article(article)[0].chunk_id
        assert first == second
        assert first.startswith("ГК РФ:art395:")

    def test_build_chunks_keeps_metadata(self) -> None:
        chunks = build_chunks(TEXT, code="ГК РФ")
        assert chunks
        assert all(chunk.code == "ГК РФ" for chunk in chunks)
        assert {chunk.article for chunk in chunks} == {"1", "2", "454"}

    def test_short_fragments_dropped(self) -> None:
        article = LawArticle(number="1", title="Название", text="Коротко.")
        assert chunk_article(article) == []
