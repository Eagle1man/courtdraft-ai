"""Тест: заглушка не выдаёт себя за выдумку модели.

Дефект найден при подготовке демонстрации. В режиме без модели
MockLLMProvider возвращает служебную пометку с номером
(«ref=32535»), а проверка разбирала этот текст так же, как настоящий,
и объявляла номер выдуманным:

    [CRITICAL] Вводная часть
      UNSUPPORTED_CLAIM: число 32535 отсутствует в материалах дела —
      возможна выдумка модели

Выдумки не было: модель не вызывалась вовсе. Зато на демонстрации
заказчику система выглядела сломанной — и кричала CRITICAL там, где
честный ответ «раздел не сформирован, модель не подключена».

Заглушка не содержит утверждений, поэтому проверять её как текст со
смыслом незачем: в ней нет ни сумм, ни норм, ни персон.
"""

from __future__ import annotations

from app.agents.draft_agent import DraftResult, SectionDraft
from app.agents.evidence_pack import EvidenceItem, EvidencePack
from app.verification.engine import Severity, verify_draft

STUB = (
    "[MOCK-ОТВЕТ] Реальная модель не вызывалась (режим "
    "LLM_PROVIDER=mock). Это заглушка для проверки конвейера. "
    "ref=32535 Любое утверждение из этого текста не является "
    "установленным фактом."
)


def _pack() -> EvidencePack:
    return EvidencePack(
        items=[
            EvidenceItem(
                category="claims",
                text="Прошу взыскать задолженность 150 000 руб. (01_claim.txt)",
                source_document="01_claim.txt",
                source_type="иск",
                amounts=["150000"],
                order=1,
            )
        ]
    )


def _draft(*sections: SectionDraft) -> DraftResult:
    return DraftResult(
        case_number="DEMO-001",
        provider="mock",
        sections=list(sections),
    )


def _stub_section(name: str = "Вводная часть") -> SectionDraft:
    return SectionDraft(
        key="introductory",
        name=name,
        text=STUB,
        is_mock=True,
    )


class TestStubIsNotAHallucination:
    def test_no_critical_issues(self) -> None:
        report = verify_draft(_draft(_stub_section()), _pack())
        critical = [i for i in report.issues if i.severity is Severity.CRITICAL]
        assert critical == [], (
            "заглушка объявлена выдумкой модели: "
            f"{[i.reason for i in critical]}"
        )

    def test_number_not_reported_as_invented(self) -> None:
        report = verify_draft(_draft(_stub_section()), _pack())
        assert not any("32535" in i.reason for i in report.issues)

    def test_stub_reported_as_not_formed(self) -> None:
        """Честная причина одна: раздел не сформирован."""
        report = verify_draft(_draft(_stub_section()), _pack())
        reasons = [i.reason for i in report.issues]
        assert any(
            "раздел не сформирован" in reason and "модель не" in reason
            for reason in reasons
        ), reasons

    def test_stub_not_counted_as_checked_claim(self) -> None:
        """Заглушка не является утверждением, которое можно проверить."""
        report = verify_draft(_draft(_stub_section()), _pack())
        assert report.claims_checked == 0

    def test_missing_source_not_claimed_for_stub(self) -> None:
        """«В материалах нет данных для раздела» — тоже неправда.

        Материалы есть, данные есть; не сформирован сам раздел. Разница
        для судьи существенная: в первом случае надо искать документы,
        во втором — подключать модель.
        """
        report = verify_draft(_draft(_stub_section()), _pack())
        assert not any(
            "в материалах дела нет данных" in i.reason for i in report.issues
        )


class TestRealSectionsStillChecked:
    def test_real_invented_number_still_critical(self) -> None:
        """Проверка не ослаблена: настоящий текст проверяется как раньше."""
        section = SectionDraft(
            key="introductory",
            name="Вводная часть",
            text="Ответчик должен был уплатить 987 654 руб.",
            is_mock=False,
            citations=["01_claim.txt"],
        )
        report = verify_draft(_draft(section), _pack())
        critical = [i for i in report.issues if i.severity is Severity.CRITICAL]
        assert critical, "выдуманное число в настоящем разделе пропущено"
        assert any("987654" in i.reason.replace(" ", "") for i in critical)

    def test_mixed_draft(self) -> None:
        """Заглушка и настоящий раздел проверяются по-разному."""
        real = SectionDraft(
            key="operative",
            name="РЕШИЛ",
            text=(
                "Удовлетворить исковые требования истца в полном объёме: "
                "взыскать с ответчика в пользу истца 150 000 руб. 00 коп. "
                "(задолженность по договору поставки).\n\n"
                "Взыскать с ответчика в доход бюджета государственную "
                "пошлину 5 500 руб. 00 коп.\n\n"
                "Решение может быть обжаловано в апелляционную инстанцию "
                "в течение месяца со дня принятия решения в окончательной "
                "форме."
            ),
            is_mock=False,
            built_by="rules",
            citations=["01_claim.txt"],
        )
        report = verify_draft(_draft(_stub_section(), real), _pack())
        assert report.claims_checked == 1
        assert len(report.issues) == 1, [i.reason for i in report.issues]
