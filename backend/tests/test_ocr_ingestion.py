"""OCR сканированных PDF: разбор без настоящего распознавания.

Здесь проверяется логика: когда включается OCR, что попадает в текст и
метаданные, и что отказ называет причину. Сам Tesseract проверяется
отдельно — в `test_ocr_real.py`, с реальным сканом.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.ingestion import extract as extract_module
from app.ingestion.extract import ExtractionError, Page, extract_text
from app.ingestion.ocr import OcrPage, OcrResult, OcrUnavailableError


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "ocr_enabled": True,
        "ocr_min_chars": 200,
        "ocr_max_pages": 30,
        "ocr_dpi": 200,
        "ocr_languages": "rus+eng",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _empty_pdf() -> object:
    from app.ingestion.extract import ExtractedDocument

    return ExtractedDocument(
        text="",
        pages=[Page(number=1, text="")],
        page_count=1,
        metadata={"text_source": "native"},
        page_offsets=[0],
    )


def _patch_native(monkeypatch: pytest.MonkeyPatch, text: str = "") -> None:
    from app.ingestion.extract import ExtractedDocument

    def fake_pdf(data: bytes, limit: int) -> ExtractedDocument:
        return ExtractedDocument(
            text=text,
            pages=[Page(number=1, text=text)],
            page_count=1,
            metadata={"text_source": "native"},
            page_offsets=[0],
        )

    monkeypatch.setattr(extract_module, "_extract_pdf", fake_pdf)


class TestScannedPdf:
    def test_ocr_text_replaces_empty_layer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_native(monkeypatch)
        captured: dict[str, object] = {}

        def fake_ocr(data: bytes, **kwargs: object) -> OcrResult:
            captured.update(kwargs)
            return OcrResult(
                pages=[OcrPage(1, "ИСКОВОЕ ЗАЯВЛЕНИЕ о взыскании 150 000 руб.")]
            )

        monkeypatch.setattr("app.ingestion.ocr.ocr_pdf", fake_ocr)

        result = extract_text(
            "scan.pdf", b"%PDF-1.4", mime_type="application/pdf", settings=_settings()
        )

        assert "150 000" in result.text
        assert result.metadata["text_source"] == "ocr"
        ocr_meta = result.metadata["ocr"]
        assert isinstance(ocr_meta, dict)
        assert ocr_meta["used"] is True
        assert ocr_meta["engine"] == "tesseract"
        # Языки и DPI переданы из настроек, а не зашиты в код.
        assert captured["languages"] == "rus+eng"
        assert captured["dpi"] == 200

    def test_native_text_long_enough_is_not_ocred(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_native(monkeypatch, text="Договор поставки. " * 40)

        def fail(*args: object, **kwargs: object) -> OcrResult:
            raise AssertionError("текстовый PDF не должен идти в OCR")

        monkeypatch.setattr("app.ingestion.ocr.ocr_pdf", fail)
        result = extract_text(
            "contract.pdf", b"%PDF-1.4", mime_type="application/pdf", settings=_settings()
        )
        assert result.metadata["text_source"] == "native"
        assert "Договор поставки" in result.text

    def test_unavailable_ocr_names_the_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_native(monkeypatch)

        def no_ocr(*args: object, **kwargs: object) -> OcrResult:
            raise OcrUnavailableError("Tesseract не найден")

        monkeypatch.setattr("app.ingestion.ocr.ocr_pdf", no_ocr)
        with pytest.raises(ExtractionError) as exc:
            extract_text(
                "scan.pdf", b"%PDF-1.4", mime_type="application/pdf", settings=_settings()
            )
        message = str(exc.value)
        assert "скана" in message or "сканированного" in message or "скан" in message
        assert "Tesseract не найден" in message

    def test_ocr_disabled_is_stated_explicitly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_native(monkeypatch)
        with pytest.raises(ExtractionError) as exc:
            extract_text(
                "scan.pdf",
                b"%PDF-1.4",
                mime_type="application/pdf",
                settings=_settings(ocr_enabled=False),
            )
        assert "OCR" in str(exc.value)

    def test_ocr_without_text_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_native(monkeypatch)

        def empty_ocr(*args: object, **kwargs: object) -> OcrResult:
            return OcrResult(pages=[OcrPage(1, "")])

        monkeypatch.setattr("app.ingestion.ocr.ocr_pdf", empty_ocr)
        with pytest.raises(ExtractionError):
            extract_text(
                "scan.pdf", b"%PDF-1.4", mime_type="application/pdf", settings=_settings()
            )

    def test_ocr_warnings_are_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_native(monkeypatch)

        def low_quality(*args: object, **kwargs: object) -> OcrResult:
            return OcrResult(
                pages=[OcrPage(1, "короткий текст")],
                warnings=["страница 1: распознано слишком мало текста"],
            )

        monkeypatch.setattr("app.ingestion.ocr.ocr_pdf", low_quality)
        result = extract_text(
            "scan.pdf", b"%PDF-1.4", mime_type="application/pdf", settings=_settings()
        )
        ocr_meta = result.metadata["ocr"]
        assert isinstance(ocr_meta, dict)
        assert ocr_meta["warnings"] == ["страница 1: распознано слишком мало текста"]
