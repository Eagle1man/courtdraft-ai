"""Заголовок документа отсекается, а содержание — нет.

Заголовки брались из предложений первых строк без ограничения длины
самой строки. В документе, набранном в одну строку, такими «заголовками»
становились все фразы до первого числа — и документ переставал давать
в пакет ни одного факта (четыре документа из четырёх пустовали,
падали и тесты, и живое дело).
"""

from __future__ import annotations

import hashlib
import uuid

from app.agents.evidence_pack import build_evidence_pack
from app.models.entities import CaseDocument


def make_document(filename: str, text: str, doc_type: str) -> CaseDocument:
    return CaseDocument(
        id=uuid.uuid4(),
        case_id=uuid.uuid4(),
        filename=filename,
        document_type=doc_type,
        mime_type="text/plain",
        page_count=1,
        text=text,
        metadata_json={},
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def build(documents: list[CaseDocument]):
    return build_evidence_pack("T-0002", "Тестовый суд", "договорные споры", documents)


class TestSingleLineDocument:
    """Документ в одну строку должен давать факты, а не пустоту."""

    def test_facts_are_kept(self) -> None:
        pack = build(
            [
                make_document(
                    "03_contract.txt",
                    "Договор поставки № 14 заключён между Образцовым и ООО "
                    "«Фирма Вымышленная Строй». Цена договора составляет "
                    "120 000 руб. Оплата производится в течение десяти дней.",
                    "CONTRACT",
                )
            ]
        )
        assert pack.items, "документ в одну строку не должен терять факты"

    def test_money_sentence_survives(self) -> None:
        pack = build(
            [
                make_document(
                    "01_claim.txt",
                    "Прошу взыскать задолженность по договору поставки в "
                    "размере 150 000 руб. 00 коп.",
                    "CLAIM",
                )
            ]
        )
        texts = [item.text for item in pack.items]
        assert any("150 000" in text for text in texts)


class TestHeadingLineSkipped:
    """Отдельная строка-название в начале файла — не факт дела."""

    def test_heading_is_not_a_fact(self) -> None:
        pack = build(
            [
                make_document(
                    "07_response.txt",
                    "Возражение на исковое заявление\n"
                    "Ответчик не согласен с размером долга, поскольку "
                    "товар оплачен частично в размере 40 000 руб.",
                    "RESPONSE",
                )
            ]
        )
        texts = [item.text.lower() for item in pack.items]
        assert not any("возражение на исковое" in text for text in texts)

    def test_short_line_with_amount_is_not_a_heading(self) -> None:
        # Короткая строка со суммой — содержание, а не название.
        pack = build(
            [
                make_document(
                    "05_calculation.txt",
                    "Расчёт процентов\n12 000 руб. начислено за период",
                    "OTHER",
                )
            ]
        )
        texts = [item.text for item in pack.items]
        assert any("12 000" in text for text in texts)
