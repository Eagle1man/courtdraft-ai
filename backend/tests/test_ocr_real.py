"""OCR настоящего скана: Tesseract, PyMuPDF и русский язык.

Тест создаёт PDF **без текстового слоя** — страницу с изображением — и
проверяет, что текст из неё действительно извлекается. Если Tesseract
или языки не установлены, тест пропускается: отсутствие внешней
программы не должно ронять набор, но и притворяться, что OCR проверен,
нельзя.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.ingestion.extract import extract_text
from app.ingestion.ocr import availability

ROOT = Path(__file__).resolve().parents[1]
LOCAL_TESSDATA = ROOT / ".tessdata"


def _tessdata_dir() -> str:
    if LOCAL_TESSDATA.exists():
        return LOCAL_TESSDATA.as_posix()
    return ""


def _skip_reason() -> str:
    ok, reason = availability("rus+eng", _tessdata_dir())
    return "" if ok else reason


SKIP = _skip_reason()


def _scanned_pdf() -> bytes:
    """PDF, где текст нарисован как картинка: текстового слоя нет."""
    import pymupdf

    source = pymupdf.open()
    page = source.new_page(width=595, height=842)
    font = "helv"
    font_file = Path("C:/Windows/Fonts/arial.ttf")
    if font_file.exists():
        page.insert_font(fontname="F0", fontfile=str(font_file))
        font = "F0"
    page.insert_text(
        (72, 100),
        "ИСКОВОЕ ЗАЯВЛЕНИЕ",
        fontsize=18,
        fontname=font,
    )
    page.insert_text(
        (72, 140),
        "о взыскании задолженности 150000 рублей",
        fontsize=14,
        fontname=font,
    )
    pixmap = page.get_pixmap(dpi=150)
    image = pixmap.tobytes("png")
    source.close()

    scanned = pymupdf.open()
    image_page = scanned.new_page(width=595, height=842)
    image_page.insert_image(image_page.rect, stream=image)
    data = scanned.tobytes()
    scanned.close()
    return data


@pytest.mark.skipif(bool(SKIP), reason=SKIP or "OCR недоступен")
class TestRealOcr:
    def test_image_only_pdf_is_recognized(self) -> None:
        settings = Settings(
            _env_file=None,
            ocr_enabled=True,
            ocr_languages="rus+eng",
            ocr_tessdata_dir=_tessdata_dir(),
            ocr_dpi=200,
            ocr_min_chars=200,
        )
        result = extract_text(
            "scan.pdf",
            _scanned_pdf(),
            mime_type="application/pdf",
            settings=settings,
        )

        assert result.metadata["text_source"] == "ocr"
        text = result.text.lower()
        # Проверяем не дословность (OCR ошибается), а то, что текст
        # действительно прочитан: слова и сумма на месте.
        assert "исков" in text or "заявлен" in text, result.text
        assert "150000" in text.replace(" ", "") or "150 000" in text, result.text

    def test_text_layer_pdf_is_not_ocred(self) -> None:
        import pymupdf

        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((72, 100), "Supply contract 150000", fontsize=14)
        data = document.tobytes()
        document.close()

        settings = Settings(
            _env_file=None,
            ocr_min_chars=5,
            ocr_tessdata_dir=_tessdata_dir(),
        )
        result = extract_text(
            "contract.pdf", data, mime_type="application/pdf", settings=settings
        )
        assert result.metadata["text_source"] == "native"
        assert "Supply" in result.text
