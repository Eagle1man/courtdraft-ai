"""Разбор текста кодекса на статьи и пассажи для базы знаний.

Зачем: модель должна ссылаться на нормы, которые есть в кодексе, а не
на выдуманные. Для этого нужен не список номеров, а тексты статей с
метаданными: кодекс, статья, название, часть. Только так поиск может
вернуть «ГК РФ, ст. 395» и передать модели сам текст нормы.

Разбор простой и проверяемый: заголовок статьи в тексте кодекса —
отдельная строка «Статья N. Название». Длинные статьи режутся на
пассажи с перекрытием: короткий фрагмент точнее находится поиском, но
без перекрытия определение нормы рвётся на границе.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Заголовок статьи: «Статья 395. Ответственность за неисполнение…».
_ARTICLE_HEADING = re.compile(
    r"(?m)^\s*Статья\s+(\d+(?:\.\d+)?)\s*\.\s*([^\n]*)$"
)

#: Части кодекса: «Часть первая», «Часть четвёртая».
_PART = re.compile(
    r"Часть\s+(перв|втор|трет|четв[её]рт)\w*",
    re.IGNORECASE,
)

_PART_NAMES = {
    "перв": "Часть первая",
    "втор": "Часть вторая",
    "трет": "Часть третья",
    "четв": "Часть четвёртая",
}

#: Слишком короткий пассаж не нужен: заголовок статьи без текста.
MIN_CHUNK = 60

#: Служебная шапка страницы PDF-издания: повторяется на каждой странице
#: и вклинивается в текст статьи. Без удаления в нормы попадают
#: «Дата обновления», «WWW.GARANT.RU» и строка правообладателя.
_PAGE_FURNITURE = re.compile(
    r"(?mi)^[ \t]*(?:"
    r"Дата\s+(?:обновления|актуализации):.*|"
    r"Информация\s+предоставлена.*|"
    r"WWW\.GARANT\.RU[ \t]*|"
    r"©[ \t]*ООО.*|"
    r"Компания[ \t]+.*?с[ \t]+1990.*|"
    r"Система[ \t]+ГАРАНТ.*"
    r")[ \t]*$"
)


def strip_page_furniture(raw: str) -> str:
    """Убрать повторяющуюся шапку страниц PDF-издания.

    Это не часть закона: колонтитул издателя. Оставлять его нельзя —
    он разрывает определение нормы и попадает в подсказку модели.

    Неразрывные пробелы заменяются до поиска: в PDF «Дата обновления»
    набрано именно ими, и обычный пробел в шаблоне не совпадал.
    """
    if not raw:
        return ""
    normalized = raw.replace("\u00a0", " ").replace("\u200b", "")
    cleaned = _PAGE_FURNITURE.sub("", normalized)
    # Пустые блоки после удаления склеиваются, лишние пустые строки
    # схлопываются: иначе на месте шапки остаётся «дыра» из переводов.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


@dataclass(frozen=True)
class LawArticle:
    """Статья кодекса целиком."""

    number: str
    title: str
    text: str
    part: str = ""


@dataclass(frozen=True)
class LawChunk:
    """Пассаж статьи — единица поиска."""

    chunk_id: str
    code: str
    article: str
    title: str
    part: str
    text: str
    order: int = 0


def normalize_law_text(raw: str) -> str:
    """Склеить переносы строк внутри абзаца.

    PDF рвёт строку посреди предложения, и без склейки модель получает
    текст с обрывами: «граждане (физические лица),\\nюридические лица».
    Пустая строка остаётся границей абзаца.
    """
    if not raw:
        return ""
    text = raw.replace("\u00a0", " ").replace("\u00ad", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"(?<![.!?…:])\n(?!\n)", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_articles(raw_text: str, *, code: str = "ГК РФ") -> list[LawArticle]:
    """Разобрать текст кодекса на статьи.

    Заголовок статьи — единственный надёжный разделитель: нумерация
    пунктов и подпунктов внутри статьи на него не похожа.
    """
    source = strip_page_furniture(raw_text or "")
    matches = list(_ARTICLE_HEADING.finditer(source))
    articles: list[LawArticle] = []

    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        body = normalize_law_text(source[start:end])
        title = match.group(2).strip().rstrip(".")
        # Часть кодекса — последнее упоминание перед статьёй.
        part_matches = list(_PART.finditer(source, 0, match.start()))
        part = ""
        if part_matches:
            part = _PART_NAMES.get(part_matches[-1].group(1).lower(), "")
        articles.append(
            LawArticle(
                number=match.group(1),
                title=title,
                text=body,
                part=part,
            )
        )
    return articles


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.;!?…])\s+|\n+", text)
    return [part.strip() for part in parts if part.strip()]


def chunk_article(
    article: LawArticle,
    *,
    code: str = "ГК РФ",
    max_chars: int = 1200,
    overlap: int = 150,
) -> list[LawChunk]:
    """Нарезать статью на пассажи с перекрытием.

    Перекрытие нужно, чтобы определение нормы не терялось на границе:
    поиск возвращает один пассаж, и без хвоста предыдущего он может
    начинаться с «…по этим правилам».
    """
    sentences = _split_sentences(article.text)
    if not sentences:
        return []

    chunks: list[str] = []
    current: list[str] = []
    length = 0

    for sentence in sentences:
        if current and length + len(sentence) > max_chars:
            chunks.append(" ".join(current))
            tail = " ".join(current)[-overlap:]
            current = [tail] if tail else []
            length = len(tail)
        current.append(sentence)
        length += len(sentence) + 1

    if current:
        chunks.append(" ".join(current))

    result: list[LawChunk] = []
    for order, text in enumerate(chunks):
        cleaned = text.strip()
        if len(cleaned) < MIN_CHUNK:
            continue
        result.append(
            LawChunk(
                chunk_id=f"{code}:art{article.number}:{order:03d}",
                code=code,
                article=article.number,
                title=article.title,
                part=article.part,
                text=cleaned,
                order=order,
            )
        )
    return result


def build_chunks_from_articles(
    articles: list[LawArticle],
    *,
    code: str = "ГК РФ",
    max_chars: int = 1200,
    overlap: int = 150,
) -> list[LawChunk]:
    """Нарезать на пассажи уже разобранные статьи.

    Отдельно от `build_chunks`: текст кодекса можно получить не только
    из PDF, но и из выгрузки статей (JSONL). Разбор и нарезка не должны
    зависеть от того, откуда взялся текст.
    """
    chunks: list[LawChunk] = []
    for article in articles:
        chunks.extend(
            chunk_article(article, code=code, max_chars=max_chars, overlap=overlap)
        )
    return chunks


def build_chunks(
    raw_text: str,
    *,
    code: str = "ГК РФ",
    max_chars: int = 1200,
    overlap: int = 150,
) -> list[LawChunk]:
    """Разобрать кодекс и нарезать все статьи на пассажи."""
    return build_chunks_from_articles(
        parse_articles(raw_text, code=code),
        code=code,
        max_chars=max_chars,
        overlap=overlap,
    )


def articles_to_records(articles: list[LawArticle], *, code: str) -> list[dict[str, str]]:
    """Статьи в записи JSONL: кодекс, номер, название, текст."""
    return [
        {
            "code": code,
            "article": article.number,
            "title": article.title,
            "part": article.part,
            "text": article.text,
        }
        for article in articles
    ]


def articles_from_records(
    records: list[dict[str, str]], *, code: str = "ГК РФ"
) -> list[LawArticle]:
    """Записи JSONL обратно в статьи.

    Кодекс в записи важнее аргумента: в одном файле могут лежать статьи
    разных кодексов, и подменять их одним значением нельзя.
    """
    articles: list[LawArticle] = []
    for record in records:
        articles.append(
            LawArticle(
                number=str(record.get("article", "")),
                title=str(record.get("title", "")),
                text=str(record.get("text", "")),
                part=str(record.get("part", "")),
            )
        )
    return articles
