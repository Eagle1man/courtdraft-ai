"""Разбор eval-кейсов пакета DeepSeek v1.1 против нашей реализации.

Источник — пакет ``deepseek_json_20261026_ae3ad0.json``, раздел ``eval``
(28 кейсов). Кейсы перенесены сюда как зафиксированные входы: тест
должен жить в репозитории и не зависеть от файла разбора на чужом
диске. Каждая проверка сверяется с реальной функцией проекта, а не с
тем, что пакет о ней написал.

Кейсы, которые здесь НЕ перенесены, и почему:

* ``EVAL-006``, ``EVAL-U04`` (allocate_costs) — пакет считает долю
  пошлины при частичном удовлетворении. Наш продукт этого не считает:
  размер пропорциональной пошлины остаётся расчётом судьи и в
  резолютивной идёт с пометкой, что сумма требует расчёта (см.
  ``app.drafting.operative``).
* ``EVAL-007``, ``EVAL-008`` (propose_outcome) — пакет предлагает исход
  спора. Исход определяет судья: система даёт карту доказательств
  (``subject_of_proof``), а не вердикт.
* ``EVAL-U05`` (fee_status) — отдельной ставки для ИП нет: НК РФ
  (ст. 333.19) считает ИП как физическое лицо, и наша ``state_fee``
  не спрашивает статус. Проверка совпадает с ``EVAL-004``.
* ``EVAL-001`` покрыт частично: цена 150 000 руб. не попадает под
  мировой суд (П2), пошлина сходится. Строки про П4/П6 (досудебный
  порядок, претензия) не имеют собственной функции в проекте.
"""

from __future__ import annotations

from app.agents.evidence_pack import EvidenceItem
from app.verification.engine import IssueType
from app.verification.forbidden_refs import check_forbidden_refs
from app.verification.jurisdiction import expected_tier
from app.verification.laws import fee_matches, state_fee
from app.verification.procedure_limits import check_procedure_limits
from app.verification.qualification import check_qualification
from app.verification.resol_checks import check_resolution
from app.verification.subject_of_proof import subject_of_proof

# Из EVAL-009: частичное удовлетворение без отказа в остальной части.
PARTIAL = (
    "Удовлетворить исковые требования частично в размере 100 000 руб. "
    "Взыскать с ООО «Ритм» (ИНН: 7700112233) в пользу Лапина Артёма "
    "Геннадьевича (паспорт: 46 11 556677) 100 000,00 руб."
)

# Тот же исход, но с определённой судьбой неудовлетворённой части.
PARTIAL_DONE = (
    "Удовлетворить исковые требования частично в размере 100 000 руб. "
    "В удовлетворении остальной части иска отказать. Взыскать с "
    "ООО «Ритм» (ИНН: 7700112233) в пользу Лапина Артёма "
    "Геннадьевича (паспорт: 46 11 556677) 100 000,00 руб."
)


def _resol_ids(text: str) -> set[str]:
    return {d.id for d in check_resolution(text, {})}


def _lease_items() -> list[EvidenceItem]:
    """Вход EVAL-U07: договор аренды, акт приёма-передачи, платёжка."""
    return [
        EvidenceItem(
            category="evidence",
            text=text,
            source_document=f"{n:02d}.txt",
            source_type="EVIDENCE",
            order=n,
        )
        for n, text in enumerate(
            (
                "Договор аренды помещения от 01.03.2025",
                "Акт приёма-передачи помещения от 05.03.2025",
                "Платёжное поручение об арендной плате",
            ),
            start=1,
        )
    ]


class TestJurisdictionAndFee:
    """EVAL-001 (частично), EVAL-003, EVAL-004, EVAL-005, EVAL-U03."""

    def test_eval_003_price_30k_goes_to_world_court(self) -> None:
        assert expected_tier(30_000) == "мировой судья"

    def test_eval_001_price_150k_stays_in_district_court(self) -> None:
        # П2 «OK»: предел мирового (50 000 руб.) не достигнут.
        assert expected_tier(150_000) == "районный суд"

    def test_eval_004_fee_at_150k(self) -> None:
        assert state_fee(150_000) == 5_500.0

    def test_eval_005_fee_at_150m(self) -> None:
        assert state_fee(150_000_000) == 389_000.0

    def test_eval_u03_fee_reconciles(self) -> None:
        assert fee_matches(150_000, 5_500)

    def test_train_b1_01_fee_shortfall_does_not_reconcile(self) -> None:
        # Из train B1-01: уплачено 5 100 при расчётных 5 500 — недоплата.
        assert not fee_matches(150_000, 5_100)


class TestSubordination:
    """EVAL-002 (П1), EVAL-001 (П1 без флага)."""

    def test_eval_002_ip_vs_ip_supply_is_arbitration(self) -> None:
        findings = check_qualification(
            ["Договор поставки от 15.03.2026, товар отгружен."],
            plaintiff="ИП Иванов",
            defendant="ИП Петров",
        )
        arbitration = [f for f in findings if f.kind == "arbitration"]
        assert arbitration, [f.reason for f in findings]
        assert "ответчик — ИП" in arbitration[0].reason
        assert "27, 28 АПК" in arbitration[0].reason

    def test_eval_001_no_flags_without_ip_plaintiff(self) -> None:
        findings = check_qualification(
            ["Договор поставки № 1 от 01.02.2025, товар передан."],
            plaintiff="ООО «Вышка»",
            defendant="ИП Сидоров",
        )
        assert findings == []


class TestSimplifiedProcedure:
    """EVAL-U01 (П5), EVAL-U02 (П5)."""

    def test_eval_u01_240k_is_a_hint(self) -> None:
        findings = check_procedure_limits(
            ["Прошу взыскать задолженность по договору."], 240_000
        )
        assert [f.kind for f in findings] == ["simplified"]

    def test_eval_u02_260k_is_the_general_order(self) -> None:
        findings = check_procedure_limits(
            ["Прошу взыскать задолженность по договору."], 260_000
        )
        assert findings == []


class TestSelfCheck:
    """EVAL-009 (R02), EVAL-010 и EVAL-U06 (R13)."""

    def test_eval_009_partial_without_denial_is_violation(self) -> None:
        assert "RESOL_PARTIAL_NO_DENIAL" in _resol_ids(PARTIAL)

    def test_eval_009_partial_with_denial_is_silent(self) -> None:
        assert "RESOL_PARTIAL_NO_DENIAL" not in _resol_ids(PARTIAL_DONE)

    def test_eval_010_and_u06_foreign_code_is_critical(self) -> None:
        found = check_forbidden_refs("руководствуясь ст.309 УК РФ")
        assert [(f.kind, f.severity) for f in found] == [
            ("foreign_code", "CRITICAL")
        ]
        assert IssueType.FOREIGN_CODE.value == "FOREIGN_CODE"


class TestProofMatrix:
    """EVAL-U07 (build_proof_matrix по аренде)."""

    def test_eval_u07_lease_matrix_has_six_rows(self) -> None:
        facts = subject_of_proof(_lease_items(), kind="аренда")
        assert len(facts) == 6

    def test_eval_u07_row_for_article_622_is_covered(self) -> None:
        """Строка о просрочке возврата имущества (ст. 622 ГК РФ).

        Материалы содержат «арендную плату», но ни слова о возврате —
        строка остаётся непокрытой, пока в деле нет документа
        по ст. 622.
        """
        facts = subject_of_proof(_lease_items(), kind="аренда")
        assert facts[5].circumstance == "плата за просрочку возврата имущества"
        assert not facts[5].covered

        with_622 = _lease_items() + [
            EvidenceItem(
                category="evidence",
                text="Ст. 622 ГК РФ: возврат имущества после окончания срока.",
                source_document="04.txt",
                source_type="EVIDENCE",
                order=4,
            )
        ]
        covered = subject_of_proof(with_622, kind="аренда")
        assert covered[5].covered
        assert covered[5].docs == ("04.txt",)
