"""Проверка родовой подсудности (п. 4 и п. 5 ч. 1 ст. 23 ГПК РФ)."""

from app.verification.jurisdiction import (
    CONSUMER_LIMIT,
    WORLD_LIMIT,
    check_jurisdiction,
    expected_tier,
)


class TestExpectedTier:
    """Общий порог 50 000 руб., потребительский — 100 000 руб."""

    def test_small_claim_world_judge(self) -> None:
        assert expected_tier(50_000) == "мировой судья"

    def test_over_limit_district(self) -> None:
        assert expected_tier(50_001) == "районный суд"

    def test_zero_no_tier(self) -> None:
        assert expected_tier(0) == "районный суд"

    def test_limits_follow_the_article(self) -> None:
        # П. 4 ч. 1 ст. 23 ГПК РФ — 50 000 руб., п. 5 — 100 000 руб.
        # по спорам о защите прав потребителей.
        assert WORLD_LIMIT == 50_000
        assert CONSUMER_LIMIT == 100_000

    def test_consumer_threshold(self) -> None:
        assert expected_tier(80_000, consumer=True) == "мировой судья"
        assert expected_tier(100_001, consumer=True) == "районный суд"


class TestCheckJurisdiction:
    """Расхождение видно, совпадение молчит, отсутствие тоже молчит."""

    def test_mismatch_world_judge_over_limit(self) -> None:
        findings = check_jurisdiction(
            ["Иск подан мировому судье судебного участка № 12"],
            claim_total=150_000,
        )
        assert len(findings) == 1
        assert findings[0].expected == "районный суд"
        assert "мировой" in findings[0].found
        assert "п. 4 ч. 1 ст. 23" in findings[0].reason

    def test_mismatch_district_small_claim(self) -> None:
        findings = check_jurisdiction(
            ["Дело рассматривается районным судом"], claim_total=50_000
        )
        assert len(findings) == 1
        assert findings[0].expected == "мировой судья"

    def test_world_judge_over_general_limit_is_flagged(self) -> None:
        # Старая ошибка: порогом служил п. 5 (потребительский, 100 000),
        # и иск на 89 000 руб. у мирового судьи проходил молча.
        findings = check_jurisdiction(
            ["Мировой судья судебного участка № 123"], claim_total=89_122.19
        )
        assert len(findings) == 1
        assert findings[0].expected == "районный суд"

    def test_consumer_case_silent_under_consumer_limit(self) -> None:
        # Потребительский порог включается только по явному признаку.
        assert not check_jurisdiction(
            [
                "Иск о защите прав потребителей, "
                "подан мировому судье судебного участка № 4"
            ],
            claim_total=80_000,
        )

    def test_consumer_case_flagged_over_consumer_limit(self) -> None:
        findings = check_jurisdiction(
            [
                "Иск о защите прав потребителей, "
                "подан мировому судье судебного участка № 4"
            ],
            claim_total=150_000,
        )
        assert len(findings) == 1
        assert findings[0].expected == "районный суд"
        assert "п. 5 ч. 1 ст. 23" in findings[0].reason

    def test_match_is_silent(self) -> None:
        assert not check_jurisdiction(
            ["Мировой судья судебного участка № 3"], claim_total=40_000
        )

    def test_no_court_mention_is_silent(self) -> None:
        # Суд не назван в материалах — замечания нет: признака нет,
        # выдумывать инстанцию система не умеет.
        assert not check_jurisdiction(
            ["Истец просит взыскать долг по договору"], claim_total=150_000
        )

    def test_no_claims_is_silent(self) -> None:
        # Цена иска неизвестна — сверять нечего.
        assert not check_jurisdiction(
            ["Районный суд"], claim_total=0
        )

    def test_court_found_in_any_document(self) -> None:
        # Суд может быть назван не в первом документе дела.
        findings = check_jurisdiction(
            ["Прошу взыскать долг", "определение мирового судьи"],
            claim_total=200_000,
        )
        assert len(findings) == 1

    def test_match_in_later_document(self) -> None:
        assert not check_jurisdiction(
            ["Прошу взыскать долг", "решение районного суда"],
            claim_total=200_000,
        )
