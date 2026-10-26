"""Разбивка текста проекта на разделы.

Проект решения хранится двумя способами сразу: цельным текстом и
списком разделов со ссылками на источники. Список разделов нужен не
для красоты — по нему интерфейс показывает, откуда взялось каждое
утверждение, и без него продукт не делает ничего.

Оба представления расходятся в одном месте: судья правит текст, и
правка сохраняется целиком, а разбивка на разделы при этом терялась.
Интерфейс после правки показывал «разделов 0 · источников 0», то
есть ровно в момент, когда человек берёт проект в работу, из него
исчезали все ссылки на материалы дела.

Этот модуль восстанавливает разбивку по заголовкам разделов и
переносит источники на исправленный текст. Ничего не додумывает:
если разбить не удалось, раздел получает пометку «требует проверки»
и остаётся без источников.
"""

from __future__ import annotations

from typing import Any

#: Ключ раздела-заглушки, когда текст правки не удалось разбить.
UNSPLIT_KEY = "edited"
UNSPLIT_NAME = "Текст проекта (правка судьи)"


def _heading(name: str) -> str:
    """Заголовок раздела в том виде, в каком он пишется в тексте.

    Текст проекта собирается как ``ИМЯ РАЗДЕЛА`` + пустая строка +
    содержимое, поэтому искать надо по верхнему регистру. Сравнение
    регистронезависимое: человек, правивший текст вручную, мог
    набрать заголовок строчными.
    """
    return name.strip().upper()


def _as_sections(raw: Any) -> list[dict[str, Any]]:
    """Привести сохранённую разбивку к списку словарей.

    Раньше сюда попадал и список строк, и список словарей — формат
    не был зафиксирован, поэтому проверяем каждый элемент отдельно.
    """
    if not isinstance(raw, list):
        return []
    sections: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict) and item.get("name"):
            sections.append(dict(item))
    return sections


def _slice_by_headings(
    text: str, headings: list[str]
) -> dict[str, str]:
    """Разделить текст по заголовкам.

    Заголовок — отдельная строка, совпадающая с названием раздела.
    Сравнение идёт по всей строке: заголовок «УСТАНОВИЛ» не должен
    срабатывать на строке «УСТАНОВИЛ, ЧТО...».
    """
    found: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []

    for line in text.splitlines():
        candidate = line.strip().upper()
        if candidate in headings:
            if current is not None:
                found[current] = "\n".join(buffer).strip()
            current = candidate
            buffer = []
            continue
        if current is not None:
            buffer.append(line)

    if current is not None:
        found[current] = "\n".join(buffer).strip()
    return found


def resplit_sections(
    text: str, previous: Any
) -> list[dict[str, Any]]:
    """Перенести разбивку разделов на исправленный судьёй текст.

    Возвращает список разделов в прежнем порядке. У каждого раздела
    текст берётся из правки, а источники — из предыдущей версии.

    Раздел помечается как требующий проверки, если судья изменил его
    текст или удалил заголовок. Это не перестраховка: проверенный
    автоматически текст и текст после правки человека — разные
    утверждения, и источники к правке судьи отношения не имеют.
    Помечать их как подтверждённые было бы прямой ложью в интерфейсе.

    Если заголовков в тексте нет вовсе, возвращается один раздел со
    всем текстом, без источников и с пометкой проверки: приписать
    произвольный текст к конкретным документам нельзя.
    """
    sections = _as_sections(previous)
    if not sections:
        stripped = text.strip()
        if not stripped:
            return []
        return [
            {
                "key": UNSPLIT_KEY,
                "name": UNSPLIT_NAME,
                "text": stripped,
                "is_mock": False,
                "needs_review": True,
                "citations": [],
                "latency_ms": 0,
            }
        ]

    headings = [_heading(section["name"]) for section in sections]
    parts = _slice_by_headings(text, headings)

    if not parts:
        stripped = text.strip()
        if not stripped:
            return []
        return [
            {
                "key": UNSPLIT_KEY,
                "name": UNSPLIT_NAME,
                "text": stripped,
                "is_mock": False,
                "needs_review": True,
                "citations": [],
                "latency_ms": 0,
            }
        ]

    result: list[dict[str, Any]] = []
    for section, heading in zip(sections, headings, strict=True):
        new_text = parts.get(heading)
        changed = new_text is None or new_text.strip() != str(
            section.get("text", "")
        ).strip()
        result.append(
            {
                "key": str(section.get("key", "")),
                "name": str(section.get("name", "")),
                "text": new_text if new_text is not None else "",
                "is_mock": bool(section.get("is_mock", False)),
                "needs_review": bool(section.get("needs_review", False))
                or changed,
                "citations": [
                    str(c) for c in section.get("citations", []) or []
                ],
                "latency_ms": int(section.get("latency_ms", 0)),
                "gaps": [str(g) for g in section.get("gaps", []) or []],
            }
        )
    return result
