"""Извлечение текста из документов (Фаза 5, п.10).

Поддерживаемые форматы из ТЗ: PDF, DOCX, TXT, MD, HTML, JSON.

Важно для проекта: извлечение сохраняет **номера страниц**. Без них
provenance (п.12) неполон, а без provenance нельзя показать «источник
утверждения» — то есть теряется главное требование продукта.

Зависимости тяжёлые импортируются лениво: на ноутбуке 8 GB и при
импорте `pypdf`/BeautifulSoup каждого запроса незачем тратить память,
а `python-docx` в некоторых сборках отсутствует.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from app.config import Settings, get_settings
from app.models.entities import DocumentType

logger = logging.getLogger(__name__)

# Максимум символов, извлекаемых из одного файла. Защита от того, что
# сгенерированный или битый файл съест память (п.54).
MAX_CHARS_PER_DOCUMENT = 2_000_000


class ExtractionError(ValueError):
    """Не удалось извлечь текст.

    Отдельный тип, чтобы ingestion мог пометить документ как
    нечитаемый, а не молча продолжить с пустым текстом: пустой текст
    в проекте хуже явной ошибки, он выглядит как «документ пуст».
    """


@dataclass
class Page:
    """Страница или логический раздел документа."""

    number: int
    text: str

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


@dataclass
class ExtractedDocument:
    """Результат извлечения."""

    text: str
    pages: list[Page] = field(default_factory=list)
    page_count: int = 0
    metadata: dict[str, object] = field(default_factory=dict)
    # Позиции символов в полном тексте для каждой страницы.
    # Нужны, чтобы provenance мог указать character_start/character_end
    # в документе целиком, а не внутри страницы.
    page_offsets: list[int] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


def detect_mime_type(filename: str, provided: str = "") -> str:
    """Определить тип файла по расширению."""
    suffix = PurePosixPath(filename).suffix.lower().lstrip(".")
    known = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "txt": "text/plain",
        "md": "text/markdown",
        "html": "text/html",
        "htm": "text/html",
        "json": "application/json",
    }
    return known.get(suffix) or provided or "application/octet-stream"


def guess_document_type(filename: str, text: str) -> str:
    """Предварительно определить тип документа (п.11).

    Эвристика по заголовку документа, имени файла и первым строкам.
    Это НЕ классификация агентом — она дешёвая, делается до индексации
    и нужна, чтобы разложить документы по коллекциям.

    Порядок проверок важен и задан живым делом (1w121):

    1. Заголовок документа («Документ 5. Возражение на исковое
       заявление») — самый надёжный признак.
    2. Имя файла — только потом и без общего маркера «иск»: под него
       попадали «Возражение на исковое заявление.docx», «Опись
       приложений к исковому заявлению.docx» и «Расчёт процентов
       (приложение к иску)» — все три считались иском, и возражения
       ответчика шли в требования истца.
    3. Содержимое первых строк — для протокола и экспертного
       заключения, они решают маршрут обработки.

    Возвращает строку, а не строгий Enum: типы расширяемы (п.11).
    """
    name = filename.lower()
    raw_first = text.split("\n", 1)[0].strip().lower()
    # Заголовок — короткая первая строка документа («Документ 5.
    # Возражение на исковое заявление»). Длинная первая строка — это
    # просто первое предложение: «Прошу взыскать задолженность по
    # договору…» в иске читалось бы как договор, и весь документ
    # уходил не в ту категорию.
    header = raw_first[:400] if len(raw_first) <= 140 else ""
    head = text[:4000].lower()

    # 1. Заголовок документа.
    if "возражени" in header:
        return DocumentType.RESPONSE.value
    if "исковое заявление" in header:
        return DocumentType.CLAIM.value
    # Досудебная претензия — НЕ иск: это требование до суда, и в
    # требования истца оно попадать не должно (в материалах дела оно
    # лежит отдельным документом, см. 1w121).
    if re.search(r"ходатайств|заявлен\w*\s+о\s+рассмотрени", header):
        return DocumentType.MOTION.value
    if "протокол" in header:
        return DocumentType.PROTOCOL.value
    if "договор" in header:
        return DocumentType.CONTRACT.value
    # Приложения и описи — не самостоятельный иск, хотя имя файла их
    # содержит («Опись приложений к исковому заявлению»).
    if re.search(r"опис\w*|приложени\w*|расч[ёе]т", header):
        return DocumentType.OTHER.value

    # 2. Имя файла. Общего маркера «иск» здесь больше нет.
    by_name: list[tuple[tuple[str, ...], str]] = [
        (("claim", "pretens", "исковое", "иск."), DocumentType.CLAIM.value),
        (("response", "otzyv", "vozrazhen", "возраж"), DocumentType.RESPONSE.value),
        (("contract", "dogovor", "договор", "contract"), DocumentType.CONTRACT.value),
        (("receipt", "check", "chek", "receipt", "платёж", "чек"), DocumentType.RECEIPT.value),
        (("protocol", "протокол"), DocumentType.PROTOCOL.value),
        (("expert", "zakluchenie", "заключение", "эксперт"), DocumentType.EXPERT_OPINION.value),
        (("act", "акт"), DocumentType.ACT.value),
        (("motion", "hodataistvo", "ходатайство"), DocumentType.MOTION.value),
    ]
    for markers, doc_type in by_name:
        if any(marker in name for marker in markers):
            return doc_type

    # 3. По содержимому — для протокола и экспертного заключения: они
    # решают, пойдёт ли дело по ветке Transcript Agent и как цитировать
    # заключение, и ошибка здесь меняет весь маршрут.
    if re.search(r"\bпротокол\b.{0,40}\b(заседан|судебн)", head, re.DOTALL):
        return DocumentType.PROTOCOL.value
    if re.search(r"\bзаключение\s+(судебной\s+)?эксперт", head):
        return DocumentType.EXPERT_OPINION.value
    if re.search(r"\bисковое\s+заявление\b", head):
        return DocumentType.CLAIM.value

    return DocumentType.OTHER.value


def extract_text(
    filename: str,
    data: bytes,
    *,
    mime_type: str = "",
    settings: Settings | None = None,
) -> ExtractedDocument:
    """Извлечь текст из файла.

    Бросает ExtractionError, если текст получить не удалось или он пуст.
    """
    cfg = settings or get_settings()
    resolved = detect_mime_type(filename, mime_type)
    limit = min(cfg.max_upload_mb * 1024 * 1024, MAX_CHARS_PER_DOCUMENT)

    if resolved == "application/pdf":
        result = _extract_pdf(data, limit)
    elif resolved.startswith("application/vnd.openxmlformats"):
        result = _extract_docx(data, limit)
    elif resolved in ("text/html", "application/xhtml+xml"):
        result = _extract_html(data, limit)
    elif resolved == "application/json":
        result = _extract_json(data, limit)
    else:
        result = _extract_plain(data, limit)

    if result.is_empty:
        raise ExtractionError(
            f"Не удалось извлечь текст из {filename} "
            f"(тип {resolved}). Возможно, файл состоит из сканированных "
            f"изображений без текстового слоя."
        )

    result.metadata["detected_mime_type"] = resolved
    return result


# ── Отдельные форматы ─────────────────────────────────────────────────────


def _extract_plain(data: bytes, limit: int) -> ExtractedDocument:
    """TXT и MD. Учитываем возможную кодировку кириллицы."""
    text = _decode(data)

    # MD -> текст без служебной разметки, но с сохранением структуры:
    # заголовки оставляем, они нужны для чанкинга по структуре.
    if text.lstrip().startswith("#") or "```" in text[:2000]:
        text = _strip_markdown(text)

    text = text[:limit]
    return ExtractedDocument(text=text, pages=[Page(number=1, text=text)], page_count=1)


def _extract_pdf(data: bytes, limit: int) -> ExtractedDocument:
    """PDF с сохранением границ страниц."""
    try:
        import io

        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise ExtractionError("pypdf не установлен") from exc

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise ExtractionError(f"Не удалось прочитать PDF: {exc}") from exc

    pages: list[Page] = []
    offsets: list[int] = []
    total = 0

    for index, page in enumerate(reader.pages, start=1):
        try:
            page_text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - отдельная страница может не читаться
            logger.warning("Страница %d PDF не извлечена", index)
            page_text = ""

        page_text = clean_text(page_text)
        # Смещение — позиция страницы в ИТОГОВОМ тексте, поэтому
        # учитываем разделитель "\n\n" между страницами.
        if pages:
            total += 2
        offsets.append(total)
        pages.append(Page(number=index, text=page_text))
        total += len(page_text)

        if total >= limit:
            logger.info("PDF обрезан на странице %d (лимит %d)", index, limit)
            break

    text = "\n\n".join(p.text for p in pages)[:limit]
    return ExtractedDocument(
        text=text,
        pages=pages,
        page_count=len(pages),
        metadata={"encrypted": getattr(reader, "is_encrypted", False)},
        page_offsets=offsets,
    )


def _extract_docx(data: bytes, limit: int) -> ExtractedDocument:
    """DOCX: абзацы и заголовки с сохранением иерархии.

    Заголовки помечаются префиксом, потому что чанкер (Фаза 5) режет
    документ по структуре, а не на равные куски (п.14).
    """
    try:
        import io

        import docx
    except ImportError as exc:  # pragma: no cover
        raise ExtractionError("python-docx не установлен") from exc

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise ExtractionError(f"Не удалось прочитать DOCX: {exc}") from exc

    parts: list[str] = []
    for paragraph in document.paragraphs:
        content = (paragraph.text or "").strip()
        if not content:
            parts.append("")
            continue

        style = (paragraph.style.name or "").lower() if paragraph.style else ""
        if style.startswith("heading"):
            # Уровень заголовка: Heading 1 -> «#», Heading 2 -> «##».
            level = "".join(ch for ch in style if ch.isdigit()) or "1"
            parts.append(f"{'#' * min(int(level), 6)} {content}")
        elif style == "title":
            parts.append(f"# {content}")
        else:
            parts.append(content)

        if sum(len(p) for p in parts) >= limit:
            break

    text = clean_text("\n\n".join(parts))[:limit]
    return ExtractedDocument(
        text=text,
        pages=[Page(number=1, text=text)],
        page_count=1,
        metadata={"paragraphs": len(document.paragraphs), "tables": len(document.tables)},
    )


def _extract_html(data: bytes, limit: int) -> ExtractedDocument:
    """HTML -> текст. Скрипты и стили убираются."""
    raw = _decode(data)
    stripped = re.sub(
        r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw
    )
    # Блочные элементы -> перенос строки, иначе текст слипнется.
    stripped = re.sub(r"(?i)<br\s*/?>", "\n", stripped)
    stripped = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n\n", stripped)
    stripped = re.sub(r"(?i)<li[^>]*>", "- ", stripped)
    text = re.sub(r"<[^>]+>", " ", stripped)
    text = html.unescape(text)
    text = clean_text(text)[:limit]

    return ExtractedDocument(
        text=text,
        pages=[Page(number=1, text=text)],
        page_count=1,
    )


def _extract_json(data: bytes, limit: int) -> ExtractedDocument:
    """JSON -> читаемый текст.

    Форматируем с отступами, а не выгружаем в JSON: текст должен быть
    пригоден для чанкинга и цитирования так же, как обычный документ.
    """
    try:
        parsed = json.loads(_decode(data))
    except (ValueError, TypeError) as exc:
        raise ExtractionError(f"Некорректный JSON: {exc}") from exc

    text = clean_text(json.dumps(parsed, ensure_ascii=False, indent=2))[:limit]
    return ExtractedDocument(
        text=text,
        pages=[Page(number=1, text=text)],
        page_count=1,
        metadata={"root_type": type(parsed).__name__},
    )


# ── Общие помощники ───────────────────────────────────────────────────────

_ENCODINGS = ("utf-8", "utf-8-sig", "cp1251", "koi8-r", "latin-1")


def _decode(data: bytes) -> str:
    """Распознать кодировку.

    cp1251 до latin-1: latin-1 декодирует любой байт и не падает, но
    русский текст превращается в мусор. Поэтому сначала проверяем
    строгие кодировки и только затем откатываемся.
    """
    for encoding in _ENCODINGS:
        try:
            decoded = data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        # Строгие кодировки дают «мусор» только при неверном выборе,
        # поэтому проверяем долю кириллицы и управляющих символов.
        if encoding in ("utf-8", "utf-8-sig"):
            return decoded
        if _looks_like_russian(decoded):
            return decoded
    return data.decode("utf-8", errors="replace")


def _looks_like_russian(text: str) -> bool:
    """Похоже ли, что текст декодирован верно."""
    sample = text[:2000]
    if not sample:
        return True
    cyrillic = sum(1 for ch in sample if "а" <= ch.lower() <= "я" or ch == "ё")
    # Мусор из cp1251, прочитанный как latin-1, даёт латиницу с
    # «ёлочками» и символами вида ¤­. Считаем их мусором.
    mojibake = sum(1 for ch in sample if ch in "¤­°±²µ¶·¸¹º")
    return cyrillic >= mojibake


_WS_RE = re.compile(r"[ \t ]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")

# Невидимые символы: zero-width space/joiner/non-joiner, BOM, мягкий
# перенос строки. В тексте из PDF и DOCX они встречаются регулярно,
# ломают сравнение документов и делают SHA256 нестабильным.
#
# Заданы через кодовые точки, а не литералами: невидимый символ в
# исходнике невозможно увидеть, и при записи он молча пропадает —
# проверка на удаление проходила бы на пустом наборе.
_INVISIBLE_CODEPOINTS = (
    0x200B,  # zero-width space
    0x200C,  # zero-width non-joiner
    0x200D,  # zero-width joiner
    0xFEFF,  # BOM
    0x00AD,  # soft hyphen
    0x2060,  # word joiner
)

_INVISIBLE_CHARS = tuple(chr(cp) for cp in _INVISIBLE_CODEPOINTS)


def clean_text(text: str) -> str:
    """Нормализовать текст.

    Пробелы и переводы строк приводятся к каноническому виду, чтобы
    одни и те же документы давали одинаковые чанки и SHA256 не
    «плавал» из-за незначительных различий в форматировании.
    """
    if not text:
        return ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    # Невидимые символы, которые ломают и сравнение текстов, и хэш.
    for invisible in _INVISIBLE_CHARS:
        normalized = normalized.replace(invisible, "")
    normalized = _WS_RE.sub(" ", normalized)
    normalized = _MULTI_NEWLINE_RE.sub("\n\n", normalized)
    return normalized.strip()


def _strip_markdown(text: str) -> str:
    """Убрать Markdown-разметку, оставив структуру заголовков.

    Заголовки НЕ убираются: чанкер режет документ по разделам, и без
    заголовков фрагменты теряют контекст. Остальная разметка
    (выделение, код, ссылки) убирается, потому что она мешает
    цитированию: в цитате должен быть текст, а не «**«**».
    """
    text = re.sub(r"```[a-zA-Z]*\n(.*?)```", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Жирный и курсив: **текст**, __текст__, *текст*, _текст_
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"~~(.+?)~~", r"\1", text, flags=re.DOTALL)
    # Зачёркнутый в GFM переиспользует ~, поэтому только одиночный.
    text = re.sub(r"(?m)^\s*~\s+(.+)$", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(?m)^\s*[-*+]\s+", "- ", text)
    text = re.sub(r"(?m)^\s*>\s?", "", text)
    text = re.sub(r"(?m)^\s*([-*_])\1{2,}\s*$", "", text)
    return text
