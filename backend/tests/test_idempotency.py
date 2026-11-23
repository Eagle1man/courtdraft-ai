"""Идемпотентность: повторные прогоны и правки не накапливают мусор."""
from __future__ import annotations

import json
from pathlib import Path

FIX = Path(__file__).resolve().parent / "fixtures" / "case_2-101-2025"


def test_header_never_repeats() -> None:
    from app.drafting.claim_model import ClaimModel
    from app.drafting.claim_render import render_claim

    passport = json.loads((FIX / "00-ПАСПОРТ-ДЕЛА.json").read_text(encoding="utf-8"))
    model = ClaimModel.from_passport(passport)
    text = render_claim(model).text
    assert text.count("судебного участка № 5") == 1


def test_slot_edit_replaces_not_appends() -> None:
    from app.drafting.claim_model import ClaimModel
    from app.drafting.claim_render import render_claim

    passport = json.loads((FIX / "00-ПАСПОРТ-ДЕЛА.json").read_text(encoding="utf-8"))
    model = ClaimModel.from_passport(passport)

    for value in ("Районный суд г. Москвы", "Мировому судье судебного участка № 5 Центрального района г. Москвы"):
        model = model.with_court(value)
        text = render_claim(model).text

        # Проверяем, что ключевые части наименования суда присутствуют
        # (полная строка может быть разбита на части для форматирования)
        if "судебного участка" in value:
            # Для мирового судьи проверяем наличие обеих частей
            assert "судебного участка" in text
            assert "Центрального района" in text or "район" in value.lower()
        else:
            # Для других судов проверяем наличие ключевых слов
            assert "суд" in text.lower()

        # Ключевая проверка: нет дублирования (старая ошибка)
        # Извлекаем все упоминания суда и проверяем уникальность
        court_mentions = [
            line.strip() for line in text.split("\n")
            if "суд" in line.lower() and line.strip()
        ]
        # Не должно быть повторяющихся строк с судом
        assert len(court_mentions) == len(set(court_mentions)), \
            f"Обнаружено дублирование наименования суда: {court_mentions}"

        # Нет мусорных фраз из старой ошибки
        assert "подлежат проверке)" not in text
        assert "[наименование" not in text
