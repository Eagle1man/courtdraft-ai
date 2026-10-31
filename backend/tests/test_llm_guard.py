"""Разбор ответа промпт-классификатора инструкций."""

from __future__ import annotations

from app.ingestion.llm_guard import (
    BENIGN_LABEL,
    INJECTION_LABEL,
    UNKNOWN_LABEL,
    parse_label,
)


class TestParseLabel:
    def test_plain_answer(self) -> None:
        assert parse_label("INJECTION") == INJECTION_LABEL
        assert parse_label("BENIGN") == BENIGN_LABEL

    def test_answer_with_thinking_tag(self) -> None:
        assert parse_label(" response\n\nBENIGN") == BENIGN_LABEL

    def test_last_mention_wins(self) -> None:
        """Модель рассуждает и называет оба слова: ответ — последнее."""
        raw = "Should I answer INJECTION or BENIGN? The text is a normal claim, so BENIGN."
        assert parse_label(raw) == BENIGN_LABEL

    def test_unknown_answer(self) -> None:
        assert parse_label("Okay, let's see.") == UNKNOWN_LABEL
        assert parse_label("") == UNKNOWN_LABEL
