"""Тесты заочного производства в резолютивной части.

Заочное производство — не редкий случай. Разбор 1 425 гражданских
резолюций показал: 698 из них (49,0 %) содержат признаки заочного
рассмотрения. До этого код не знал о нём вовсе, и резолютивная часть
строилась так, будто ответчик всегда явился.

Юридическая ошибка тут не косметическая: заочное решение без указания
на надлежащее извещение и неуважительность неявки может быть отменено,
а без упоминания права на отмену ответчик лишён этого права.
"""

from __future__ import annotations

import pytest

from app.agents.evidence_pack import EvidenceItem, EvidencePack
from app.drafting.operative import (
    MoneyClaim,
    Procedure,
    build_operative,
)
from app.drafting.requirements import extract_procedure


def claim() -> MoneyClaim:
    return MoneyClaim(
        amount=150000,
        basis="задолженность по договору поставки",
        source="договор поставки № 14",
    )


class TestDefaultJudgmentItems:
    """Пункты, которые обязаны быть в заочном решении."""

    def test_legal_basis_item_first(self) -> None:
        """Вводная формула открывает резолютивную часть.

        Без неё резолюция начиналась сразу с «Удовлетворить…» и не
        читалась как часть решения.
        """
        result = build_operative([claim()], fee_paid=5100,
                                 procedure=Procedure.DEFAULT)
        assert result.items[0].startswith("Руководствуясь")
        assert "ст. 198" in result.items[0]

    def test_legal_basis_printed_once(self) -> None:
        """Вводная формула печатается один раз.

        Она стоит первым элементом items и выводится как вводная к
        пунктам, а не отдельной строкой и пунктом: в каждой резолюции
        преамбула печаталась дважды.
        """
        result = build_operative([claim()], fee_paid=5100,
                                 procedure=Procedure.DEFAULT)
        text = result.render()
        assert text.count("Руководствуясь") == 1
        assert text.startswith("Руководствуясь")
        assert "\n1. " in text

    def test_procedure_item_second(self) -> None:
        """Порядок рассмотрения идёт после вводной формулы."""
        result = build_operative([claim()], fee_paid=5100,
                                 procedure=Procedure.DEFAULT)
        assert result.items[1].startswith("Рассмотреть дело")
        assert "заочного производства" in result.items[1]

    def test_procedure_cites_article_233(self) -> None:
        """Статья 233 ГПК РФ — основание заочного производства."""
        result = build_operative([claim()], fee_paid=5100,
                                 procedure=Procedure.DEFAULT)
        assert "ст. 233 ГПК РФ" in result.items[1]

    def test_non_appearance_reason_stated(self) -> None:
        """Суд обязан признать причину неявки неуважительной."""
        result = build_operative([claim()], fee_paid=5100,
                                 procedure=Procedure.DEFAULT)
        assert "неуважительн" in result.items[1]

    def test_cancellation_right_stated(self) -> None:
        """Право ответчика на отмену заявлением за 7 дней."""
        result = build_operative([claim()], fee_paid=5100,
                                 procedure=Procedure.DEFAULT)
        text = "\n".join(result.items)
        assert "отмен" in text
        assert "семи дней" in text

    def test_appeal_term_differs_from_ordinary(self) -> None:
        """У заочного решения другой срок обжалования.

        Обычное решение обжалуется в течение месяца со дня принятия;
        заочное — по истечении срока подачи заявления об отмене.
        Смешение этих сроков лишает ответчика права на обжалование.
        """
        default = build_operative([claim()], fee_paid=5100,
                                  procedure=Procedure.DEFAULT)
        ordinary = build_operative([claim()], fee_paid=5100,
                                   procedure=Procedure.ORDINARY)

        assert any("заочное решение может быть обжаловано" in i.lower()
                   for i in default.items)
        assert any("Решение может быть обжаловано" in i
                   for i in ordinary.items)

    def test_consequence_cites_part_one_article_237(self) -> None:
        """Срок на отмену — ч. 1 ст. 237 ГПК РФ, а не ст. 233.

        Ст. 233 ГПК устанавливает само заочное производство и сроков
        не содержит; семь дней со дня вручения — ч. 1 ст. 237
        (сверено по официальному тексту, 2026-10-24).
        """
        result = build_operative([claim()], fee_paid=5100,
                                 procedure=Procedure.DEFAULT)
        consequence = next(i for i in result.items if "семи дней" in i)
        assert "ч. 1 ст. 237 ГПК РФ" in consequence
        assert "ст. 233" not in consequence

    def test_appeal_cites_part_two_article_237(self) -> None:
        """Апелляция на заочное решение — ч. 2 ст. 237 ГПК РФ, а не ст. 332.

        Ст. 332 ГПК описывает последствия отмены заочного решения и к
        обжалованию отношения не имеет: ссылка на неё в сроке на
        апелляцию лишала жалобу процессуального основания.
        """
        result = build_operative([claim()], fee_paid=5100,
                                 procedure=Procedure.DEFAULT)
        appeal = next(i for i in result.items
                      if "заочное решение может быть обжаловано" in i.lower())
        assert "ч. 2 ст. 237 ГПК РФ" in appeal
        assert "ст. 332" not in appeal

    def test_legal_basis_includes_article_233(self) -> None:
        """Основание дополняется ст. 233 автоматически."""
        result = build_operative([claim()], fee_paid=5100,
                                 procedure=Procedure.DEFAULT)
        assert result.render().startswith(
            "Руководствуясь ст. 198, 233 ГПК РФ, суд решил:"
        )

    def test_ordinary_basis_unchanged(self) -> None:
        result = build_operative([claim()], fee_paid=5100,
                                 procedure=Procedure.ORDINARY)
        assert result.render().startswith(
            "Руководствуясь ст. 198 ГПК РФ, суд решил:"
        )

    def test_no_cancellation_right_in_ordinary(self) -> None:
        """В обычном производстве права на отмену нет."""
        result = build_operative([claim()], fee_paid=5100,
                                 procedure=Procedure.ORDINARY)
        assert not any("семи дней" in i for i in result.items)

    def test_claims_still_present(self) -> None:
        """Порядок не вытесняет разрешение по требованиям."""
        result = build_operative([claim()], fee_paid=5100,
                                 procedure=Procedure.DEFAULT)
        text = "\n".join(result.items)
        assert "150 000" in text
        assert "взыскать с ответчика" in text


class TestAbsentPlaintiff:
    """Неявка истца — ст. 167 ГПК РФ, это НЕ заочное производство."""

    def test_absent_plaintiff_item(self) -> None:
        result = build_operative([claim()], fee_paid=5100,
                                 procedure=Procedure.ABSENT_PLAINTIFF)
        assert result.items[1].startswith("Рассмотреть дело в отсутствие")
        assert "истц" in result.items[1]

    def test_absent_plaintiff_has_no_cancellation(self) -> None:
        """У неявки истца нет семидневного срока отмены."""
        result = build_operative([claim()], fee_paid=5100,
                                 procedure=Procedure.ABSENT_PLAINTIFF)
        assert not any("семи дней" in i for i in result.items)


class TestProcedureFromMaterials:
    """Порядок определяется по материалам, а не по умолчанию."""

    def _pack(self, *texts: str) -> EvidencePack:
        pack = EvidencePack(case_number="DEMO-001")
        for index, text in enumerate(texts, start=1):
            pack.items.append(
                EvidenceItem(
                    category="protocol_findings",
                    text=text,
                    source_document="05_protocol.txt",
                    source_type="PROTOCOL",
                    order=index,
                )
            )
        return pack

    def test_default_detected(self) -> None:
        pack = self._pack(
            "Ответчик не явился в судебное заседание; суд определил "
            "рассмотреть дело в порядке заочного производства."
        )
        assert extract_procedure(pack) is Procedure.DEFAULT

    def test_ordinary_by_default(self) -> None:
        """Без указания на порядок остаётся обычный."""
        pack = self._pack("Ответчик явился, представитель поддержал иск.")
        assert extract_procedure(pack) is Procedure.ORDINARY

    def test_absent_plaintiff_not_confused_with_default(self) -> None:
        """«В отсутствие истца» — ст. 167, а не заочное производство."""
        pack = self._pack("Суд определил рассмотреть дело в отсутствие истца.")
        assert extract_procedure(pack) is Procedure.ABSENT_PLAINTIFF

    def test_empty_pack_gives_ordinary(self) -> None:
        assert extract_procedure(EvidencePack()) is Procedure.ORDINARY


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_draft_contains_default_procedure(self) -> None:
        """Сквозная проверка: заочное производство доходит до проекта."""
        from app.agents.draft_agent import generate_draft

        class NoLLM:
            name = "noop"
            _model = ""

            async def generate(self, request: object):  # type: ignore[override]
                raise AssertionError("модель не должна вызываться")

            async def health(self) -> bool:
                return False

            @property
            def model_name(self) -> str:
                return "noop"

        pack = EvidencePack(case_number="DEMO-001")
        pack.items.append(
            EvidenceItem(
                category="claims",
                text="Взыскать с ответчика задолженность по договору "
                     "поставки в размере 150000 руб.",
                source_document="01_claim.txt",
                source_type="CLAIM",
                order=1,
            )
        )
        pack.items.append(
            EvidenceItem(
                category="protocol_findings",
                text="Суд определил рассмотреть дело в порядке заочного "
                     "производства в отсутствие ответчика.",
                source_document="05_protocol.txt",
                source_type="PROTOCOL",
                order=2,
            )
        )

        template = {
            "sections": [
                {"key": "operative", "name": "РЕШИЛ",
                 "purpose": "", "required_fields": []},
            ]
        }
        result = await generate_draft(pack, template, provider=NoLLM())
        text = result.sections[0].text
        assert "заочного производства" in text
        assert "семи дней" in text
