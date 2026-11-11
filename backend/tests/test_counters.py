"""Сверка счётчиков отчёта и источников (п. 9.5 разбора)."""

import asyncio

from app.agents.draft_agent import DraftResult, SectionDraft, generate_draft
from app.agents.evidence_pack import EvidenceItem, EvidencePack
from app.verification.engine import IssueType, verify_draft


class TestSourceCounters:
    def test_one_document_counts_once(self) -> None:
        # Рецензент: один документ посчитан как два источника — ссылались
        # на него с разных страниц, и дедупликация по полной строке
        # считала это двумя документами.
        result = DraftResult(case_number="T-1")
        result.sections = [
            SectionDraft(
                key="motivating",
                name="Мотивировочная часть",
                text="Договор подтверждён.",
                is_mock=False,
                citations=["03_contract.txt", "03_contract.txt, стр. 3"],
            ),
            SectionDraft(
                key="descriptive",
                name="Описательная часть",
                text="Договор заключён.",
                is_mock=False,
                citations=["03_contract.txt, стр. 7"],
            ),
        ]
        assert result.sources() == ["03_contract.txt"]

    def test_stats_citations_match_sources(self) -> None:
        result = DraftResult(case_number="T-2")
        result.sections = [
            SectionDraft(
                key="operative",
                name="РЕШИЛ",
                text="Взыскать.",
                is_mock=False,
                citations=["01_claim.txt", "01_claim.txt, стр. 2", "02_response.txt"],
            )
        ]
        assert result.stats()["citations"] == 2


class TestEmptySection:
    def test_empty_section_flagged_and_counted(self) -> None:
        pack = EvidencePack(case_number="T-3")
        draft = type("D", (), {})()
        draft.sections = [
            SectionDraft(key="introductory", name="Вводная часть", text="", is_mock=False)
        ]
        report = verify_draft(draft, pack)
        empty = [
            i
            for i in report.issues
            if i.type is IssueType.INCOMPLETE and "пуст" in i.reason
        ]
        assert empty
        assert report.unsupported_claims >= 1

    def test_nonempty_section_not_flagged_as_empty(self) -> None:
        pack = EvidencePack(case_number="T-4")
        draft = type("D", (), {})()
        draft.sections = [
            SectionDraft(
                key="introductory",
                name="Вводная часть",
                text="Именем Российской Федерации",
                is_mock=False,
            )
        ]
        report = verify_draft(draft, pack)
        empty = [i for i in report.issues if "пуст" in i.reason]
        assert not empty

    def test_counters_agree_in_full_run(self) -> None:
        pack = EvidencePack(case_number="T-5", court_name="Мировой судья")
        pack.items.append(
            EvidenceItem(
                category="claims",
                text="Взыскать 150 000 руб.",
                source_document="01_claim.txt",
                source_type="CLAIM",
                order=1,
            )
        )
        from app.agents.evidence_pack import load_template

        template = load_template(name="civil_judgment")

        class _NoLLM:
            name = "no-llm"
            model = ""

            async def generate(self, request):  # noqa: ANN001, ANN201
                return type("R", (), {"text": "Текст раздела по материалам.", "is_mock": False})()

        result = asyncio.run(generate_draft(pack, template, provider=_NoLLM()))
        report = verify_draft(result, pack)
        data = report.to_dict()
        # Реальные инварианты, а не подгонка: sections_checked всегда
        # равно числу разделов, а claims_checked не может его превысить —
        # оно растёт один раз на незаглушечный раздел.
        assert data["sections_checked"] == len(result.sections)
        assert data["claims_checked"] <= data["sections_checked"]


class TestModelUsage:
    def test_actual_model_and_tokens_are_recorded(self) -> None:
        """Демо и T5 обязаны видеть модель, которая реально ответила."""
        pack = EvidencePack(case_number="T-6", court_name="Мировой судья")
        pack.items.append(
            EvidenceItem(
                category="claims",
                text="Взыскать 150 000 руб.",
                source_document="01_claim.txt",
                source_type="CLAIM",
                order=1,
            )
        )
        from app.agents.evidence_pack import load_template

        template = load_template(name="civil_judgment")

        class _Usage:
            prompt_tokens = 31
            completion_tokens = 17

        class _Response:
            text = (
                "В силу статьи 310 ГК РФ обязательства должны исполняться "
                "надлежащим образом и односторонний отказ от исполнения "
                "недопустим. Материалами дела подтверждено, что ответчик "
                "получил товар и оплату не произвёл. Довод ответчика о "
                "ненадлежащем качестве товара отклоняется: доказательств "
                "этого обстоятельства в силу ст. 56 ГПК РФ не представлено. "
                "На основании изложенного требование о взыскании "
                "задолженности подлежит удовлетворению в полном размере."
            )
            is_mock = False
            model = "deepseek/test-model"
            usage = _Usage()

        class _LLM:
            name = "openai"

            async def generate(self, request):  # noqa: ANN001, ANN201
                return _Response()

        result = asyncio.run(
            generate_draft(
                pack,
                template,
                provider=_LLM(),
                only_sections=["motivating"],
            )
        )
        assert len(result.sections) == 1
        section = result.sections[0]
        assert section.model == "deepseek/test-model"
        assert section.prompt_tokens == 31
        assert section.completion_tokens == 17
        assert result.model == "deepseek/test-model"
