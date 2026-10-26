"""База законодательства и расчёт госпошлины.

Две проверки, которых не было:

1. Ссылка на норму подтверждается базой законодательства, а не
   материалами дела. Суд применяет ст. 309 ГК — в иске её нет, и раньше
   каждая верная ссылка помечалась неподтверждённой.
2. Госпошлина сверяется с расчётной по цене иска (НК РФ, ст. 333.19).
   Сумма из иска могла быть указана неверно, и это видно только при
   расчёте.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.agents.evidence_pack import EvidenceItem, EvidencePack
from app.config import Settings
from app.drafting.operative import MoneyClaim, build_operative
from app.verification.engine import verify_draft
from app.verification.laws import (
    LawRegistry,
    extract_fee,
    state_fee,
    state_fee_order,
)


def make_pack(*texts: str) -> EvidencePack:
    pack = EvidencePack(case_number="T-0001")
    for index, text in enumerate(texts, start=1):
        pack.items.append(
            EvidenceItem(
                category="claims",
                text=text,
                source_document="01_claim.txt",
                source_type="CLAIM",
                order=index,
            )
        )
    return pack


ROOT = Path(__file__).resolve().parent.parent


def settings() -> Settings:
    # Путь абсолютный: pytest запускается из backend/, и относительный
    # «data/laws» разрешался бы в backend/data/laws, которого нет.
    return Settings(
        app_env="development",
        laws_dir=str(ROOT / "data" / "laws"),
    )


class TestLawRegistry:
    """Нормы загружаются и находятся."""

    def test_loads_gpk(self) -> None:
        registry = LawRegistry.load(settings().laws_dir)
        assert registry.contains("ГПК", "198")

    def test_loads_gk(self) -> None:
        registry = LawRegistry.load(settings().laws_dir)
        assert registry.contains("ГК", "309")

    def test_absent_article(self) -> None:
        registry = LawRegistry.load(settings().laws_dir)
        assert not registry.contains("ГК", "999")

    def test_missing_reason(self) -> None:
        registry = LawRegistry.load(settings().laws_dir)
        assert "отсутствует" in registry.missing("ГК", "999")


class TestLawCheckAgainstBase:
    """Ссылка на существующую норму не требует подтверждения материалом."""

    def test_real_law_not_flagged(self) -> None:
        """Ст. 309 ГК существует — замечания быть не должно."""
        pack = make_pack("Истец просит взыскать задолженность по договору.")
        operative = build_operative(
            [MoneyClaim(amount=150000, basis="задолженность по договору",
                        source="01_claim.txt")],
            legal_basis="ст. 309, 310 ГК РФ",
        )
        from app.agents.draft_agent import SectionDraft
        draft = type("D", (), {})()
        draft.sections = [
            SectionDraft(key="operative", name="Резолютивная часть",
                         text="\n".join(operative.items), is_mock=False)
        ]
        report = verify_draft(draft, pack, settings=settings())
        law_issues = [i for i in report.issues if i.type.value == "LAW_NOT_FOUND"]
        assert not law_issues

    def test_invented_law_flagged(self) -> None:
        """Ст. 999 ГК не существует — замечание должно быть."""
        pack = make_pack("Истец просит взыскать задолженность по договору.")
        operative = build_operative(
            [MoneyClaim(amount=150000, basis="задолженность по договору",
                        source="01_claim.txt")],
            legal_basis="ст. 999 ГК РФ",
        )
        from app.agents.draft_agent import SectionDraft
        draft = type("D", (), {})()
        draft.sections = [
            SectionDraft(key="operative", name="Резолютивная часть",
                         text="\n".join(operative.items), is_mock=False)
        ]
        report = verify_draft(draft, pack, settings=settings())
        law_issues = [i for i in report.issues if i.type.value == "LAW_NOT_FOUND"]
        assert law_issues


class TestStateFeeCalculation:
    """Расчёт пошлины по НК РФ — тарифы проверены на 2026-10-19."""

    def test_fee_for_150000_after_2024(self) -> None:
        # Новая редакция: 4 000 + 3% от 50 000 = 5 500
        # (то же число, что назвал рецензент по ст. 333.19).
        assert state_fee(150_000) == 5_500

    def test_fee_for_250000(self) -> None:
        # Пример с zakonrf.info: 4 000 + 3% от 150 000 = 8 500.
        assert state_fee(250_000) == 8_500

    def test_fee_for_100000(self) -> None:
        assert state_fee(100_000) == 4_000

    def test_fee_for_20000(self) -> None:
        assert state_fee(20_000) == 4_000

    def test_fee_zero(self) -> None:
        assert state_fee(0) == 0

    def test_fee_cap_900000(self) -> None:
        # 314 000 + 0,15% от 300 000 000 = 764 000 — ещё под максимумом.
        assert state_fee(400_000_000) == 764_000
        # 314 000 + 0,15% от от 786 666 667 — выше 900 000 → максимум.
        assert state_fee(900_000_000) == 900_000  # noqa: PLR2004 - предел

    def test_fee_before_2024_uses_old_tariff(self) -> None:
        # Прежняя редакция: 3 200 + 2% от 50 000 = 4 200.
        assert state_fee(150_000, date(2020, 5, 1)) == 4_200

    def test_fee_before_2024_minimum(self) -> None:
        # Прежняя редакция: 4% цены иска, но не менее 400.
        assert state_fee(15_000, date(2020, 1, 1)) == 600
        assert state_fee(5_000, date(2015, 1, 1)) == 400

    def test_fee_before_2024_cap_60000(self) -> None:
        # 13 200 + 0,5% от 15 000 000 = 88 200 → не больше 60 000.
        assert state_fee(16_000_000, date(2023, 1, 1)) == 60_000

    def test_fee_edition_boundary_2024(self) -> None:
        # П. 28 ст. 19 ФЗ-259: новая сетка — для исков, направленных
        # в суд после 08.09.2024. Якоря: 80 000 → прежняя 800+3%×60 000
        # = 2 600, новая — фиксированные 4 000.
        assert state_fee(80_000, date(2024, 9, 8)) == 2_600  # noqa: PLR2004 - якорь
        assert state_fee(80_000, date(2024, 9, 9)) == 4_000  # noqa: PLR2004 - якорь

    def test_fee_old_edition_anchors(self) -> None:
        # Якоря прежней редакции: 4% от 10 400 = 416, от 11 100 = 444.
        assert state_fee(10_400, date(2020, 1, 1)) == 416  # noqa: PLR2004 - якорь
        assert state_fee(11_100, date(2020, 1, 1)) == 444  # noqa: PLR2004 - якорь

    def test_fee_pre2010_edition(self) -> None:
        # Редакция 2001 года: 4% цены иска, но не менее 200 руб.;
        # 30 000 → 400 + 3% от 20 000 = 1 000; потолок — 20 000.
        assert state_fee(8_000, date(2005, 1, 1)) == 320  # noqa: PLR2004 - якорь
        assert state_fee(2_000, date(2005, 1, 1)) == 200  # noqa: PLR2004 - якорь
        assert state_fee(30_000, date(2005, 1, 1)) == 1_000  # noqa: PLR2004 - якорь
        assert state_fee(40_000_000, date(2005, 1, 1)) == 20_000  # noqa: PLR2004 - якорь

    def test_fee_pre2010_boundary(self) -> None:
        # 28.01.2010 — ещё сетка 2001 года: 1 600 + 2% от 30 000 = 2 200;
        # 29.01.2010 — сетка 2010 года: 800 + 3% от 60 000 = 2 600.
        assert state_fee(80_000, date(2010, 1, 28)) == 2_200  # noqa: PLR2004 - якорь
        assert state_fee(80_000, date(2010, 1, 29)) == 2_600  # noqa: PLR2004 - якорь

    def test_order_fee_half(self) -> None:
        assert state_fee_order(150_000) == 2_750


class TestFeeCheck:
    """Пошлина в проекте сверяется с расчётной."""

    def _draft(self, fee_text: str):
        from app.agents.draft_agent import SectionDraft
        draft = type("D", (), {})()
        draft.sections = [
            SectionDraft(key="operative", name="Резолютивная часть",
                         text=fee_text, is_mock=False)
        ]
        return draft

    def test_matching_fee_not_flagged(self) -> None:
        pack = make_pack("Взыскать задолженность по договору в размере 150 000 руб. 00 коп.")
        draft = self._draft(
            "Взыскать с ответчика в пользу истца государственную пошлину "
            "в размере 5 500 руб. (ст. 98 ГПК РФ)."
        )
        report = verify_draft(draft, pack, settings=settings())
        fee_issues = [
            i for i in report.issues
            if "пошлин" in i.reason and "расчётной" in i.reason
        ]
        assert not fee_issues

    def test_wrong_fee_flagged(self) -> None:
        """5 100 вместо расчётных 4 700 — замечание."""
        pack = make_pack("Взыскать задолженность по договору в размере 150 000 руб. 00 коп.")
        draft = self._draft(
            "Взыскать с ответчика в пользу истца государственную пошлину "
            "в размере 5 100 руб. (ст. 98 ГПК РФ)."
        )
        report = verify_draft(draft, pack, settings=settings())
        fee_issues = [
            i for i in report.issues
            if "пошлин" in i.reason and "расчётной" in i.reason
        ]
        assert fee_issues

    def test_fee_extraction(self) -> None:
        assert extract_fee("пошлину в размере 5 100 руб.") == 5100
        assert extract_fee("пошлина 4 700 руб. 00 коп.") == 4700
        assert extract_fee("без пошлины") is None

    def _dated_pack(self, date_str: str, fee_text: str):
        pack = make_pack(
            "Взыскать задолженность по договору в размере 150 000 руб. 00 коп."
        )
        pack.dates = [date_str] if date_str else []
        draft = self._draft(fee_text)
        return draft, pack

    def test_archive_date_uses_old_tariff(self) -> None:
        # Материалы датируют дело 2020 годом: расчётная пошлина 4 200
        # по прежней редакции. Указано 5 500 (новый тариф) — расхождение.
        draft, pack = self._dated_pack(
            "5 марта 2020 года",
            "Взыскать с ответчика государственную пошлину "
            "в размере 5 500 руб. (ст. 98 ГПК РФ).",
        )
        report = verify_draft(draft, pack, settings=settings())
        fee_issues = [
            i for i in report.issues if "расчётной" in i.reason and "2020" in i.reason
        ]
        assert fee_issues

    def test_archive_date_old_fee_not_flagged(self) -> None:
        # Дело 2020 года, пошлина 4 200 — верная по прежней редакции.
        draft, pack = self._dated_pack(
            "5 марта 2020 года",
            "Взыскать с ответчика государственную пошлину "
            "в размере 4 200 руб. (ст. 98 ГПК РФ).",
        )
        report = verify_draft(draft, pack, settings=settings())
        fee_issues = [
            i for i in report.issues
            if "расчётной" in i.reason and "пошлин" in i.reason
        ]
        assert not fee_issues

    def test_no_date_current_tariff_still_applies(self) -> None:
        # Даты в материалах нет — считаем по действующей редакции:
        # при цене иска 150 000 расчётные 5 500, а в проекте 6 500.
        draft, pack = self._dated_pack(
            "",
            "Взыскать с ответчика государственную пошлину "
            "в размере 6 500 руб. (ст. 98 ГПК РФ).",
        )
        report = verify_draft(draft, pack, settings=settings())
        fee_issues = [i for i in report.issues if "расчётной" in i.reason]
        assert fee_issues
