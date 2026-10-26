"""Вводная часть правилами, а не моделью (п. 11.1 разбора, ч. 2 ст. 198 ГПК)."""

import asyncio

from app.agents.draft_agent import generate_draft
from app.agents.evidence_pack import EvidenceItem, EvidencePack

TEMPLATE = {
    "name": "test",
    "sections": [
        {
            "key": "introductory",
            "name": "Вводная часть",
            "purpose": "реквизиты судебного акта",
            "evidence_categories": ["case_metadata", "participants"],
        }
    ],
}


def _pack(*, with_sides: bool = True, with_claims: bool = True) -> EvidencePack:
    pack = EvidencePack(
        case_number="2-1234/2026",
        court_name="Мировой судья судебного участка № 1",
    )
    if with_sides:
        pack.participants.extend(
            [
                "Образцов Пётр Петрович (истец)",
                "ООО «Фирма-Трейд» (ответчик)",
            ]
        )
    if with_claims:
        pack.items.append(
            EvidenceItem(
                category="claims",
                text="Взыскать задолженность по договору в размере 150 000 руб.",
                source_document="01_claim.txt",
                source_type="CLAIM",
                order=1,
            )
        )
    return pack


class _NoLLM:
    name = "no-llm"
    model = ""

    async def generate(self, request):  # noqa: ANN001, ANN201
        raise AssertionError("вводная часть не должна обращаться к модели")


class TestIntroSection:
    def test_requisites_from_pack(self) -> None:
        result = asyncio.run(generate_draft(_pack(), TEMPLATE, provider=_NoLLM()))
        section = result.sections[0]
        assert section.key == "introductory"
        assert section.is_mock is False
        assert "Именем Российской Федерации" in section.text
        assert "Мировой судья судебного участка № 1" in section.text
        assert "Дело № 2-1234/2026" in section.text
        assert "Истец: Образцов Пётр Петрович" in section.text
        assert "Ответчик: ООО «Фирма-Трейд»" in section.text
        assert "Предмет спора: Взыскать задолженность" in section.text

    def test_unavailable_requisites_not_in_text(self) -> None:
        # Состав суда, секретарь и дата — их нет в материалах: в тексте
        # проекта нет ни пометки «ТРЕБУЕТ ПРОВЕРКИ», ни выдуманных
        # реквизитов. Нехватка уходит в блок «Не хватает данных».
        result = asyncio.run(generate_draft(_pack(), TEMPLATE, provider=_NoLLM()))
        section = result.sections[0]
        assert section.needs_review is True
        assert "ТРЕБУЕТ ПРОВЕРКИ" not in section.text
        assert "Состав суда" not in section.text
        assert any("Состав суда" in gap for gap in section.gaps)
        assert any("Секретарь судебного заседания" in gap for gap in section.gaps)
        assert any("Дата принятия решения" in gap for gap in section.gaps)

    def test_missing_sides_not_in_text(self) -> None:
        result = asyncio.run(
            generate_draft(
                _pack(with_sides=False, with_claims=False), TEMPLATE, provider=_NoLLM()
            )
        )
        section = result.sections[0]
        assert "ТРЕБУЕТ ПРОВЕРКИ" not in section.text
        assert any("Истец" in gap for gap in section.gaps)
        assert any("Ответчик" in gap for gap in section.gaps)
        assert any("Предмет спора" in gap for gap in section.gaps)
