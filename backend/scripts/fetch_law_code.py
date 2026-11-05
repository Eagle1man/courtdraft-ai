"""Скачать текст кодекса постатейно и сохранить в JSONL.

Зачем отдельный загрузчик: база знаний по нормам нужна не только по
Гражданскому кодексу. Для процессуальных норм (ГПК, АПК) модель обязана
ссылаться на статьи, которых у неё в индексе нет, — и она придумывает их
сама. Нужен текст.

Почему не PDF: прошлый опыт показал, что в PDF чужого издания в текст
норм вклинивается колонтитул издателя, и его приходится вычищать
эвристикой. Здесь нормы берутся из HTML, где текст статьи отделён от
навигации разметкой, — и это проверяемо: заголовок «Статья N» и текст
под ним лежат под одним контейнером.

Источник — общедоступные страницы КонсультантПлюс (robots.txt не
запрещает `/document/`). Скачанное в репозиторий не попадает: как и PDF
ГК РФ, текст лежит в `data/laws/sources/` и исключён из git.

Запуск:
    python -m scripts.fetch_law_code --code "ГПК РФ" \\
        --url https://www.consultant.ru/document/cons_doc_LAW_39570/ \\
        --output data/laws/sources/gpk_rf.jsonl
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime

import requests
from bs4 import BeautifulSoup

sys.stdout.reconfigure(errors="replace")

#: Заголовок статьи: «Статья 1. Законодательство о гражданском судопроизводстве».
_ARTICLE = re.compile(r"Статья\s+(\d+(?:\.\d+)?)\s*\.\s*(.*)")
#: Заголовок в тексте страницы: «ГПК РФ Статья 1. …» или «Статья 1. …».
#: Именно с начала абзаца: внутри нормы бывают ссылки «статья 395», и
#: принимать их за заголовок нельзя.
_HEADING = re.compile(
    r"^(?:[А-ЯЁ][А-ЯЁ\s]{0,12}?РФ\s+)?Статья\s+(\d+(?:\.\d+)?)\s*\.\s*(.*)$"
)
#: Редакция документа: «(ред. от 04.08.2026)».
_EDITION = re.compile(r"\(ред\.\s*от\s*(\d{2}\.\d{2}\.\d{4})\)")

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; courtdraft-ai internal)"}

#: Меньше этого текст статьи считать нечем: значит, разметка изменилась.
MIN_ARTICLE = 40

#: Служебные вставки КонсультантПлюс: «(в ред. …)», «(см. текст …)».
_NOISE_SELECTORS = (".doc-edit", ".doc-insert", "script", "style")


@dataclass(frozen=True)
class ArticleRecord:
    article: str
    title: str
    text: str


@dataclass(frozen=True)
class ParseOutcome:
    """Результат разбора страницы: норма, отменённая норма или причина."""

    record: ArticleRecord | None = None
    repealed: bool = False
    reason: str = ""


def _get(url: str, *, attempts: int = 3) -> str:
    """Загрузить страницу с повторами: сеть рвётся, а обрыв здесь дорог."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            response = requests.get(url, headers=_HEADERS, timeout=40)
            if response.status_code == 200:
                response.encoding = response.apparent_encoding
                return response.text
            last = RuntimeError(f"код {response.status_code}")
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"не удалось загрузить {url}: {last}")


def _visible(node: object) -> str:
    text = node.get_text(" ", strip=True)  # type: ignore[attr-defined]
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    text = re.sub(r"\s+", " ", text).strip()
    # Ссылки внутри нормы разбивают строку пробелом: «Кодексом , другими».
    # Правило простое и безопасное — пробел перед знаком препинания не
    # несёт смысла, а после разметки появляется всегда.
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    return text.strip()


def collect_article_links(toc_html: str, *, document: str) -> dict[str, str]:
    """Ссылки на статьи из оглавления: номер → адрес.

    Раздел и глава тоже содержат слово «Статья» в названии ссылки, поэтому
    берутся только ссылки, начинающиеся с «Статья N.», и только внутри
    этого же документа.
    """
    soup = BeautifulSoup(toc_html, "lxml")
    prefix = f"/document/{document}/"
    links: dict[str, str] = {}
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not href.startswith(prefix) or "#" in href:
            continue
        match = _ARTICLE.match(_visible(anchor))
        if match is None:
            continue
        links.setdefault(match.group(1), "https://www.consultant.ru" + href)
    return links


def parse_article(html: str, *, article: str) -> ParseOutcome:
    """Вытащить норму из страницы статьи.

    Текст берётся только из контейнера документа: в нём нет шапки, меню и
    рекламного блока «полный текст». Служебные вставки о редакциях
    удаляются — это метаданные издания, а не содержание нормы.
    """
    soup = BeautifulSoup(html, "lxml")
    content = soup.select_one("div.document-page__content")
    if content is None:
        return ParseOutcome(reason="нет контейнера документа")

    for selector in _NOISE_SELECTORS:
        for node in content.select(selector):
            node.decompose()

    paragraphs = [_visible(node) for node in content.find_all(["p", "h1", "h2", "h3"])]
    paragraphs = [text for text in paragraphs if text]
    if not paragraphs:
        return ParseOutcome(reason="пустая страница")

    title = ""
    body: list[str] = []
    for index, text in enumerate(paragraphs):
        match = _HEADING.match(text)
        if match is not None and match.group(1) == article:
            title = match.group(2).strip().rstrip(".")
            body = paragraphs[index + 1 :]
            break
    else:
        # У части статей заголовок вынесен в <h1>, а первый абзац — уже
        # текст нормы. Тогда бережём текст, а название оставляем пустым.
        body = paragraphs

    text = "\n\n".join(body).strip()
    # «Утратила силу» — это не сбой разбора, а состояние нормы: в индекс
    # такие статьи не идут, чтобы модель не сослалась на отменённую норму
    # как на действующую.
    if title.lower().startswith("утратила силу"):
        return ParseOutcome(repealed=True, reason="утратила силу")
    if not text:
        return ParseOutcome(reason="нет текста")

    return ParseOutcome(record=ArticleRecord(article=article, title=title, text=text))


def _edition(html: str) -> str:
    match = _EDITION.search(html)
    return match.group(1) if match else ""


def _sort_key(record: ArticleRecord) -> tuple[int, int]:
    """Порядок статей как в кодексе: 6, 6.1, 10, а не 10, 6, 6.1.

    Сравнение строк дало бы «10» < «6»; номер и подпункт сравниваются
    числами.
    """
    base, _, suffix = record.article.partition(".")
    return int(base), int(suffix) if suffix.isdigit() else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", required=True, help="название кодекса, например «ГПК РФ»")
    parser.add_argument("--url", required=True, help="оглавление документа")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0, help="только первые N статей")
    args = parser.parse_args()

    document = args.url.rstrip("/").rsplit("/", 1)[-1]
    toc = _get(args.url)
    links = collect_article_links(toc, document=document)
    if args.limit:
        links = dict(list(links.items())[: args.limit])

    edition = _edition(toc)
    print(f"кодекс:        {args.code}")
    print(f"документ:      {document}")
    print(f"редакция:      {edition or 'не указана'}")
    print(f"статей в оглавлении: {len(links)}")

    records: list[ArticleRecord] = []
    failures: list[str] = []
    repealed: list[str] = []

    def _load(item: tuple[str, str]) -> tuple[str, ParseOutcome]:
        number, href = item
        try:
            return number, parse_article(_get(href), article=number)
        except Exception as exc:  # noqa: BLE001
            return number, ParseOutcome(reason=str(exc))

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for number, outcome in pool.map(_load, links.items()):
            if outcome.repealed:
                repealed.append(number)
                continue
            record = outcome.record
            if record is None:
                failures.append(f"ст. {number}: {outcome.reason or 'нет текста'}")
                continue
            if len(record.text) < MIN_ARTICLE:
                failures.append(f"ст. {number}: текст короче {MIN_ARTICLE} знаков")
                continue
            records.append(record)

    records.sort(key=_sort_key)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
    with args.output.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(
                json.dumps(
                    {
                        "code": args.code,
                        "article": record.article,
                        "title": record.title,
                        "part": "",
                        "text": record.text,
                        "edition": edition,
                        "source": f"{args.url}#{record.article}",
                        "fetched_at": fetched_at,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    lengths = [len(record.text) for record in records]
    numbered = sorted(int(record.article.split(".")[0]) for record in records)
    gaps = sorted(set(range(1, (numbered[-1] if numbered else 0) + 1)) - set(numbered))

    print(f"сохранено:     {len(records)} статей → {args.output}")
    print(f"без текста:    {len(failures)}")
    for line in failures[:15]:
        print("  ", line)
    if repealed:
        shown = ", ".join(repealed[:10])
        tail = "…" if len(repealed) > 10 else ""
        print(f"утратили силу: {len(repealed)} (ст. {shown}{tail}) — в индекс не идут")
    if lengths:
        print(f"медиана статьи: {statistics.median(lengths):.0f} знаков")
        print(f"самая короткая: {min(lengths)} знаков")
    if gaps:
        print(f"нет в выдаче:  ст. {', '.join(str(number) for number in gaps[:20])}")
    if len(records) + len(repealed) < len(links):
        raise SystemExit("НЕ ПОЛНО: часть статей не разобрана")


if __name__ == "__main__":
    main()
