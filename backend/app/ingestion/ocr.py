"""OCR сканированных PDF: локально, без облака.

Зачем: скан без текстового слоя раньше давал `characters = 0` и документ
не приносил в проект ничего, хотя выглядел загруженным. Пустой документ
и документ, который не смогли прочитать, — разные факты, и молчать о
втором нельзя.

Реализация намеренно узкая:

- страницы растеризует PyMuPDF (без внешних программ);
- распознаёт Tesseract с языками из `OCR_LANGUAGES` (по умолчанию
  `rus+eng`): для русских судебных документов он даёт читаемый текст, а
  весит десятки мегабайт вместе с языками;
- если Tesseract или язык не установлены, поднимается `OcrUnavailableError`
  с конкретной причиной — вызывающий код обязан показать её, а не
  оставить документ пустым.

Почему не модель-транскрайбер: OCR — это не генерация, здесь нельзя
«додумать» слово. Tesseract ошибается предсказуемо, а его ошибки видны
в тексте и не выглядят как факты дела.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # noqa: S404 - проверка версии и языков Tesseract
from dataclasses import dataclass, field
from pathlib import Path

ENGINE = "tesseract"


class OcrUnavailableError(RuntimeError):
    """OCR невозможен: нет программы, библиотеки или языка."""


@dataclass(frozen=True)
class OcrPage:
    number: int
    text: str


@dataclass
class OcrResult:
    pages: list[OcrPage] = field(default_factory=list)
    engine: str = ENGINE
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(page.text for page in self.pages)


def _tesseract_binary() -> str:
    """Путь к Tesseract: PATH, затем стандартное место установки.

    Установщик добавляет каталог в PATH, но уже запущенные процессы этого
    не видят — без второго варианта OCR «не находился» бы до перезапуска
    терминала, хотя программа установлена.
    """
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ):
        if Path(candidate).exists():
            return candidate
    return ""


def _installed_languages(binary: str, tessdata_dir: str = "") -> set[str]:
    command = [binary, "--list-langs"]
    if tessdata_dir:
        command.extend(["--tessdata-dir", tessdata_dir])
    try:
        completed = subprocess.run(  # noqa: S603 - путь получен проверкой
            command,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, ValueError):
        return set()
    lines = (completed.stdout or "").splitlines()
    return {line.strip() for line in lines if line.strip() and " " not in line.strip()}


def availability(languages: str, tessdata_dir: str = "") -> tuple[bool, str]:
    """(можно ли распознавать, причина отказа). Ничего не распознаёт."""
    binary = _tesseract_binary()
    if not binary:
        return False, (
            "Tesseract не найден. Установите его и язык rus "
            "(см. docs/manual-testing.md, раздел про сканы)"
        )
    try:
        import pymupdf  # noqa: F401
        import pytesseract  # type: ignore[import-untyped]  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        return False, (
            f"нет библиотеки для OCR: {exc}. Нужны pymupdf, pillow, pytesseract"
        )

    installed = _installed_languages(binary, tessdata_dir)
    requested = {part.strip() for part in languages.split("+") if part.strip()}
    missing = sorted(requested - installed)
    if missing:
        where = tessdata_dir or "каталоге установки Tesseract"
        return False, (
            f"в {where} нет языков: {', '.join(missing)}; "
            f"есть: {', '.join(sorted(installed)) or 'ничего'}"
        )
    return True, ""


def ocr_pdf(
    data: bytes,
    *,
    languages: str = "rus+eng",
    dpi: int = 200,
    max_pages: int = 30,
    tessdata_dir: str = "",
) -> OcrResult:
    """Распознать PDF постранично. Бросает OcrUnavailableError с причиной."""
    ok, reason = availability(languages, tessdata_dir)
    if not ok:
        raise OcrUnavailableError(reason)

    import pymupdf
    import pytesseract  # noqa: F401
    from PIL import Image

    binary = _tesseract_binary()
    if binary:
        pytesseract.pytesseract.tesseract_cmd = binary

    # TESSDATA_PREFIX, а не --tessdata-dir: pytesseract на Windows собирает
    # аргументы через shlex(posix=False) и оставляет кавычки внутри пути,
    # из-за чего Tesseract ищет файл «"D:/..."/rus.traineddata».
    previous_prefix = os.environ.get("TESSDATA_PREFIX")
    if tessdata_dir:
        os.environ["TESSDATA_PREFIX"] = tessdata_dir
    try:
        try:
            document = pymupdf.open(stream=data, filetype="pdf")  # type: ignore[no-untyped-call]
        except Exception as exc:  # noqa: BLE001 - повреждённый PDF
            raise OcrUnavailableError(f"PDF не открывается для OCR: {exc}") from exc

        result = OcrResult()
        try:
            pages = list(document)  # type: ignore[call-overload]
            if len(pages) > max_pages:
                result.warnings.append(
                    f"PDF содержит {len(pages)} страниц, распознаны первые "
                    f"{max_pages}: остальные не вошли в текст"
                )
            for index, page in enumerate(pages[:max_pages], start=1):
                # 200 dpi — компромисс: мельче теряются тонкие штрихи цифр,
                # крупнее OCR на CPU заметно медленнее без выигрыша.
                pixmap = page.get_pixmap(dpi=dpi)
                image = Image.frombytes(
                    "RGB", (pixmap.width, pixmap.height), pixmap.samples
                )
                text = pytesseract.image_to_string(image, lang=languages)
                text = " ".join(text.split())
                if len(text) < 20:
                    result.warnings.append(
                        f"страница {index}: распознано слишком мало текста "
                        f"({len(text)} символов) — возможно, плохой скан"
                    )
                result.pages.append(OcrPage(number=index, text=text))
        finally:
            document.close()  # type: ignore[no-untyped-call]
    finally:
        if tessdata_dir:
            if previous_prefix is None:
                os.environ.pop("TESSDATA_PREFIX", None)
            else:
                os.environ["TESSDATA_PREFIX"] = previous_prefix

    return result
