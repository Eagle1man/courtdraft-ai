"""Материалы дела: загрузка файла, просмотр текста, границы цитат.

Три дефекта, найденных при показе интерфейса владельцу:

1. Цитата в проекте решения начиналась с середины фразы: наивный разбор
   резал текст после «ст.», и вместо «Согласно ст. 56 ГПК РФ каждая
   сторона…» в раздел попадало «56 ГПК РФ каждая сторона…» — читается
   как бессмыслица.
2. Текст документа из интерфейса посмотреть было нельзя: список отдавал
   только имя и тип, сверить цитату с исходником секретарь не мог.
3. Загрузить файл можно было только прямым вызовом API — кнопки в
   интерфейсе для этого не было.

Каждый тест ниже держит один из этих контрактов.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.agents.evidence_pack import _sentences
from app.config import EmbeddingProviderName, LLMProviderName, Settings
from app.main import create_app

# Настоящая база законодательства репозитория (backend/data/laws).
_REAL_LAWS_DIR = Path(__file__).resolve().parent.parent / "data" / "laws"


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    from app.config import AppEnv

    base: dict[str, object] = {
        "app_env": AppEnv.DEVELOPMENT,
        "database_url": f"sqlite+aiosqlite:///{(tmp_path / 'api.db').as_posix()}",
        "storage_dir": tmp_path / "storage",
        "documents_dir": tmp_path / "documents",
        "demo_dir": tmp_path / "demo",
        "laws_dir": _REAL_LAWS_DIR,
        "llm_provider": LLMProviderName.MOCK,
        "embedding_provider": EmbeddingProviderName.MOCK,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def _client_for(tmp_path: Path, **overrides: object) -> AsyncIterator[AsyncClient]:
    """Клиент с временной БД и провайдерами MOCK (см. test_drafts)."""
    from app.db.bootstrap import ensure_schema
    from app.services.case_service import db

    settings = _settings(tmp_path, **overrides)
    assert await db.connect(settings), "тесту нужна рабочая БД"
    await ensure_schema()

    app = create_app(settings)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        await db.disconnect()


@pytest_asyncio.fixture
async def api_client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    async for client in _client_for(tmp_path):
        yield client


@pytest_asyncio.fixture
async def tiny_client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    """Лимит размера 0 МБ — провоцирует отказ по размеру без 50 МБ в тесте."""
    async for client in _client_for(tmp_path, max_upload_mb=0):
        yield client


async def _new_case(client: AsyncClient, number: str = "UI-001") -> str:
    response = await client.post(
        "/api/cases",
        json={
            "case_number": number,
            "court_name": "Мировой судья судебного участка № 30",
            "case_type": "civil",
            "is_synthetic": True,
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _upload(
    client: AsyncClient, case_id: str, name: str, content: bytes
) -> object:
    return await client.post(
        f"/api/cases/{case_id}/documents/upload",
        files={"file": (name, content, "application/octet-stream")},
    )


# ── Загрузка файла ─────────────────────────────────────────────────────


class TestUploadEndpoint:
    async def test_txt_upload_registers_document(self, api_client: AsyncClient) -> None:
        case_id = await _new_case(api_client)
        text = (
            "Возражения ответчика на исковое заявление о взыскании задолженности "
            "по договору поставки, изложенные в связи с предъявленными требованиями."
        )
        response = await _upload(api_client, case_id, "02_response.txt", text.encode())

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["filename"] == "02_response.txt"
        assert body["document_type"] == "RESPONSE"
        assert body["characters"] == len(text)
        assert body["has_placeholders"] is False

        detail = await api_client.get(
            f"/api/cases/{case_id}/documents/{body['id']}"
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["text"] == text

    async def test_placeholder_marks_document(self, api_client: AsyncClient) -> None:
        """Метка обезличивания в файле видна в списке без выгрузки текста."""
        case_id = await _new_case(api_client)
        content = "Чек-ордер от <ДАТА23> на оплату 3188 рублей (л.д. 204)."
        response = await _upload(
            api_client, case_id, "02_response.txt", content.encode("utf-8")
        )

        assert response.status_code == 201, response.text
        assert response.json()["has_placeholders"] is True

        listed = await api_client.get(f"/api/cases/{case_id}/documents")
        assert listed.json()[0]["has_placeholders"] is True

    async def test_duplicate_in_same_case_is_409(self, api_client: AsyncClient) -> None:
        case_id = await _new_case(api_client)
        content = "Один и тот же файл дважды в одно дело загружать не нужно."
        first = await _upload(api_client, case_id, "a.txt", content.encode())
        second = await _upload(api_client, case_id, "b.txt", content.encode())

        assert first.status_code == 201, first.text
        assert second.status_code == 409, second.text
        # Сообщение — для человека: что именно случилось и куда смотреть.
        assert "уже есть в деле" in second.json()["detail"]

    async def test_same_file_in_another_case_is_allowed(
        self, api_client: AsyncClient
    ) -> None:
        """Типовой документ законно живёт в разных делах."""
        first_case = await _new_case(api_client, "UI-001")
        second_case = await _new_case(api_client, "UI-002")
        content = "Типовой иск, встречающийся в десятках производств."

        first = await _upload(api_client, first_case, "claim.txt", content.encode())
        second = await _upload(api_client, second_case, "claim.txt", content.encode())

        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text

    async def test_unsupported_format_is_415(self, api_client: AsyncClient) -> None:
        case_id = await _new_case(api_client)
        response = await _upload(api_client, case_id, "договор.exe", b"MZ\x90\x00")

        assert response.status_code == 415, response.text
        assert "не поддерживается" in response.json()["detail"]

    async def test_empty_file_is_422(self, api_client: AsyncClient) -> None:
        case_id = await _new_case(api_client)
        response = await _upload(api_client, case_id, "пустой.txt", b"")

        assert response.status_code == 422, response.text
        assert "пустой" in response.json()["detail"]

    async def test_oversized_file_is_413(self, tiny_client: AsyncClient) -> None:
        case_id = await _new_case(tiny_client)
        response = await _upload(tiny_client, case_id, "огромный.txt", b"x" * 16)

        assert response.status_code == 413, response.text
        assert "больше" in response.json()["detail"]

    async def test_upload_to_unknown_case_is_404(self, api_client: AsyncClient) -> None:
        response = await _upload(api_client, str(uuid.uuid4()), "a.txt", "текст".encode())

        assert response.status_code == 404, response.text
        assert response.json()["detail"] == "Дело не найдено"


# ── Просмотр текста документа ──────────────────────────────────────────


class TestDocumentDetail:
    async def test_detail_returns_text_and_placeholders(
        self, api_client: AsyncClient
    ) -> None:
        case_id = await _new_case(api_client)
        content = "Ответчик представил чек-ордер от <ДАТА10> на 3188 рублей."
        uploaded = await _upload(
            api_client, case_id, "02_response.txt", content.encode("utf-8")
        )
        document_id = uploaded.json()["id"]

        detail = await api_client.get(f"/api/cases/{case_id}/documents/{document_id}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["text"] == content
        assert body["placeholders"] == ["<ДАТА10>"]

    async def test_detail_unknown_document_is_404(self, api_client: AsyncClient) -> None:
        case_id = await _new_case(api_client)
        response = await api_client.get(
            f"/api/cases/{case_id}/documents/{uuid.uuid4()}"
        )

        assert response.status_code == 404
        assert response.json()["detail"] == "Документ не найден"

    async def test_detail_document_of_another_case_is_404(
        self, api_client: AsyncClient
    ) -> None:
        """Чужой документ по своему делу не отдаётся — чужие материалы."""
        first_case = await _new_case(api_client, "UI-001")
        second_case = await _new_case(api_client, "UI-002")
        uploaded = await _upload(
            api_client, first_case, "secret.txt", "материалы первого дела".encode()
        )
        document_id = uploaded.json()["id"]

        response = await api_client.get(
            f"/api/cases/{second_case}/documents/{document_id}"
        )
        assert response.status_code == 404

    async def test_detail_unknown_case_is_404(self, api_client: AsyncClient) -> None:
        response = await api_client.get(
            f"/api/cases/{uuid.uuid4()}/documents/{uuid.uuid4()}"
        )

        assert response.status_code == 404
        assert response.json()["detail"] == "Дело не найдено"


# ── Границы цитат ──────────────────────────────────────────────────────


class TestSentenceBoundaries:
    """Цитата должна начинаться с начала фразы, а не с середины."""

    def test_abbreviation_does_not_cut_sentence(self) -> None:
        """Точка после «ст.» — не граница предложения (дефект с REAL-01)."""
        text = (
            "Согласно ст. 56 ГПК РФ каждая сторона должна доказать те "
            "обстоятельства, на которые она ссылается как на основания "
            "своих требований и возражений, если иное не предусмотрено "
            "федеральным законом. Ответчик представил в суд чек-ордер."
        )
        sentences = _sentences(text)

        assert any(s.startswith("Согласно ст. 56 ГПК РФ") for s in sentences), sentences
        assert not any(s.startswith("56 ГПК РФ") for s in sentences), sentences

    def test_list_number_is_not_a_boundary(self) -> None:
        """«1.» — нумерация пункта, а не готовое предложение-огрызок."""
        text = (
            "1. Истец неправомерно считает, что товар передан надлежащим "
            "образом, и игнорирует претензию ответчика от 10 июня."
        )
        sentences = _sentences(text)

        assert any(s.startswith("1. Истец") for s in sentences), sentences

    def test_soft_line_wrap_keeps_sentence(self) -> None:
        """Одиночный перевод строки — перенос строки, не конец фразы."""
        text = (
            "Обязанность ответчика состоит в оплате\n"
            "по договору поставки, поэтому иск подлежит удовлетворению "
            "в заявленном размере."
        )
        sentences = _sentences(text)

        assert any("оплате по договору" in s for s in sentences), sentences
