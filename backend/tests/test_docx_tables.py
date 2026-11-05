"""Извлечение таблиц из DOCX.

Проверяется то, из-за чего терялись суммы: содержимое таблиц раньше
только считалось в метаданных, а в текст документа не попадало, поэтому
расчёты задолженности и графики платежей не доходили до модели.
"""

from __future__ import annotations

import io

import docx
import pytest

from app.ingestion.extract import ExtractionError, extract_text


def _docx_bytes(builder) -> bytes:  # noqa: ANN001
    document = docx.Document()
    builder(document)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _with_table(document: docx.Document) -> None:
    document.add_paragraph("Расчёт задолженности по договору поставки.")
    table = document.add_table(rows=3, cols=3)
    table.cell(0, 0).text = "Период"
    table.cell(0, 1).text = "Долг"
    table.cell(0, 2).text = "Пени"
    table.cell(1, 0).text = "январь 2025"
    table.cell(1, 1).text = "1 000,00"
    table.cell(1, 2).text = "15,50"
    table.cell(2, 0).text = "Итого"
    table.cell(2, 1).text = "1 000,00"
    table.cell(2, 2).text = "15,50"
    document.add_paragraph("Задолженность подтверждена актом сверки.")


class TestTableExtraction:
    def test_table_cells_reach_the_text(self) -> None:
        data = _docx_bytes(_with_table)

        result = extract_text("расчёт.docx", data)

        assert "1 000,00" in result.text
        assert "15,50" in result.text
        assert "Итого" in result.text

    def test_rows_are_separated_and_labelled(self) -> None:
        data = _docx_bytes(_with_table)

        result = extract_text("расчёт.docx", data)

        assert "Таблица 1:" in result.text
        # Строка таблицы читается как строка, а не как сплошной текст.
        assert "| январь 2025 | 1 000,00 | 15,50 |" in result.text

    def test_order_of_paragraphs_and_tables(self) -> None:
        data = _docx_bytes(_with_table)

        text = extract_text("расчёт.docx", data).text

        assert text.index("Расчёт задолженности") < text.index("Таблица 1:")
        assert text.index("Таблица 1:") < text.index("актом сверки")

    def test_metadata_counts_tables_and_rows(self) -> None:
        data = _docx_bytes(_with_table)

        result = extract_text("расчёт.docx", data)

        assert result.metadata["tables"] == 1
        assert result.metadata["table_rows"] == 3

    def test_merged_cells_are_not_duplicated(self) -> None:
        def builder(document: docx.Document) -> None:
            table = document.add_table(rows=2, cols=2)
            merged = table.cell(0, 0).merge(table.cell(0, 1))
            merged.text = "Итого по договору"
            table.cell(1, 0).text = "Основной долг"
            table.cell(1, 1).text = "1 000,00"

        text = extract_text("слияние.docx", _docx_bytes(builder)).text

        assert text.count("Итого по договору") == 1

    def test_nested_table_is_extracted(self) -> None:
        def builder(document: docx.Document) -> None:
            outer = document.add_table(rows=1, cols=1)
            outer.cell(0, 0).text = "График платежей"
            inner = outer.cell(0, 0).add_table(rows=1, cols=2)
            inner.cell(0, 0).text = "01.02.2025"
            inner.cell(0, 1).text = "50 000,00"

        text = extract_text("график.docx", _docx_bytes(builder)).text

        assert "01.02.2025" in text
        assert "50 000,00" in text

    def test_document_without_tables_is_unchanged(self) -> None:
        def builder(document: docx.Document) -> None:
            document.add_paragraph("Исковое заявление о взыскании долга.")

        result = extract_text("иск.docx", _docx_bytes(builder))

        assert "Исковое заявление" in result.text
        assert result.metadata["tables"] == 0

    def test_empty_document_still_raises(self) -> None:
        def builder(document: docx.Document) -> None:  # noqa: ARG001
            return

        with pytest.raises(ExtractionError):
            extract_text("пустой.docx", _docx_bytes(builder))
