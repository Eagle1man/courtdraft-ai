"""Разбор внешних корпусов судебных решений (не часть сервиса приложения).

Модули здесь не вызываются из API: это подготовка данных для RAG и
дообучения. Вынесены в `app`, а не в `scripts`, чтобы иметь тесты.
"""

from app.datasets.court_decisions import (
    DECISION_MARKER,
    DecisionParts,
    case_kind,
    clean_decision,
    decision_date,
    extract_decision_body,
    split_sections,
)

__all__ = [
    "DECISION_MARKER",
    "DecisionParts",
    "case_kind",
    "clean_decision",
    "decision_date",
    "extract_decision_body",
    "split_sections",
]
