"""Гейт выгрузки: DOCX нельзя скачать при блокирующих замечаниях."""
from __future__ import annotations


class ExportBlockedError(RuntimeError):
    """Выгрузка запрещена. Сообщение содержит код 409 и что именно исправить."""


def assert_export_allowed(*, p0_count: int, is_stale: bool, mode: str = "final") -> None:
    if mode == "draft":
        return
    if p0_count > 0:
        raise ExportBlockedError(
            f"409: выгрузка заблокирована — блокирующих замечаний: {p0_count}. "
            "Заполните обязательные слоты и повторите проверку."
        )
    if is_stale:
        raise ExportBlockedError(
            "409: отчёт проверки относится к прежней версии текста. "
            "Нажмите «Проверить заново» и повторите выгрузку."
        )
