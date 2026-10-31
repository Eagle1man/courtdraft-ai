"""Описательная часть правилами, а не моделью.

Причина: `vikhr-7b` на этом промпте возвращает немедленный конец ответа
(`eval_count=1` на уровне API Ollama), а 1,5B пишет раздел с потерями.
Раздел собирается из материалов так же, как вводная и доводы ответчика:
только факты и дословные цитаты, без пересказа и оценок.
"""

import asyncio

from app.agents.draft_agent import generate_draft
from app.agents.evidence_pack import EvidenceItem, EvidencePack

TEMPLATE = {
    "name": "test",
    "sections": [
        {
            "key": "descriptive",
            "name": "Описательная часть",
            "purpose": "кто, к кому и с чем обратился",
            "evidence_categories": ["claims"],
        }
    ],
}


def _pack(
    *,
    with_sides: bool = True,
    with_claims: bool = True,
    with_objections: bool = False,
) -> EvidencePack:
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
    pack.documents["01_иск.txt"] = (
        "ИСКОВОЕ ЗАЯВЛЕНИЕ\n"
        "о взыскании задолженности по договору поставки\n"
        "Взыскать задолженность в размере 150 000 руб."
    )
    if with_claims:
        pack.items.append(
            EvidenceItem(
                category="claims",
                text="Взыскать задолженность в размере 150 000 руб.",
                source_document="01_иск.txt",
                source_type="CLAIM",
                order=1,
            )
        )
    if with_objections:
        pack.items.append(
            EvidenceItem(
                category="objections",
                text="Ответчик иск не признал, ссылаясь на поставку некачественного товара.",
                source_document="04_response.txt",
                source_type="RESPONSE",
                order=2,
            )
        )
    return pack


class _NoLLM:
    name = "no-llm"
    model = ""

    async def generate(self, request):  # noqa: ANN001, ANN201
        raise AssertionError("описательная часть не должна обращаться к модели")


def _section(pack: EvidencePack):  # noqa: ANN201
    result = asyncio.run(generate_draft(pack, TEMPLATE, provider=_NoLLM()))
    assert len(result.sections) == 1
    return result.sections[0]


class TestDescriptiveSection:
    def test_parties_and_subject_from_materials(self) -> None:
        section = _section(_pack())
        assert section.key == "descriptive"
        assert section.is_mock is False
        assert section.built_by == "rules"
        assert "Образцов Пётр Петрович" in section.text
        assert "Фирма-Трейд" in section.text
        assert "о взыскании задолженности по договору поставки" in section.text

    def test_claim_is_quoted_verbatim_with_document(self) -> None:
        pack = _pack()
        section = _section(pack)
        claim = pack.items[0]
        assert f"«{claim.text}»" in section.text, "требование пересказано, а не процитировано"
        assert section.citations == ["01_иск.txt"]

    def test_objections_mentioned_only_when_present(self) -> None:
        without = _section(_pack())
        assert "представил возражения" not in without.text

        with_objections = _section(_pack(with_objections=True))
        assert "представил возражения" in with_objections.text

    def test_missing_claims_are_marked_not_invented(self) -> None:
        section = _section(_pack(with_claims=False))
        assert section.needs_review is True
        # Пометка не остаётся в тексте акта: _strip_markers переносит её
        # в «Не хватает данных», а раздел остаётся чистым.
        assert any("не выделены" in gap for gap in section.gaps), section.gaps
        assert "ТРЕБУЕТ ПРОВЕРКИ" not in section.text

    def test_english_filename_still_gives_subject(self) -> None:
        """claim.pdf — обычное имя; предмет спора не должен из-за него пропадать."""
        pack = _pack()
        pack.documents = {
            "claim_01.pdf": (
                "ИСКОВОЕ ЗАЯВЛЕНИЕ\n"
                "о взыскании задолженности по договору поставки\n"
                "Взыскать задолженность в размере 150 000 руб."
            )
        }
        pack.items[0].source_document = "claim_01.pdf"
        section = _section(pack)
        assert "о взыскании задолженности по договору поставки" in section.text

    def test_no_numbers_outside_materials(self) -> None:
        """В тексте нет чисел, которых нет в пакете (главный риск правил)."""
        pack = _pack()
        section = _section(pack)
        numbers = set()
        for source in [*pack.amounts, *(item.text for item in pack.items)]:
            numbers.update(ch for ch in source if ch.isdigit())
        stray = {
            ch
            for ch in section.text
            if ch.isdigit() and ch not in numbers
        }
        # Номера пунктов списка (1., 2.) — разметка, а не факт дела.
        assert stray <= set("123456789"), stray
