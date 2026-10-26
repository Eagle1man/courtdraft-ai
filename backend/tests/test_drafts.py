"""Тесты проекта акта: сохранение, версии, проверка, эндпоинты.

Главное, что здесь проверяется, — продуктовое требование: каждая редакция
сохраняется целиком и не затирает предыдущую. Если это перестанет работать,
юридический акт нельзя будет показать в том виде, в каком он был принят.

Второе по важности — что проект недостижим без материалов дела: система
не должна составить текст по пустому делу.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import EmbeddingProviderName, LLMProviderName, Settings
from app.db.session import create_all, create_engine_and_session, drop_all
from app.main import create_app
from app.models.entities import Case, CaseStatus, DraftStatus
from app.services.draft_service import CaseNotReadyError, DraftService
from app.services.jobs import JobRegistry, JobState

# Настоящая база законодательства репозитория (backend/data/laws).
# Вычисляется вне async-фикстуры: pathlib внутри async — ASYNC240.
_REAL_LAWS_DIR = Path(__file__).resolve().parent.parent / "data" / "laws"

# ── Фикстуры ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """Движок на временной файловой SQLite.

    Файл, а не память: пул соединений SQLite в памяти создаёт отдельную
    базу на каждое соединение, а асинхронный код открывает их несколько.
    """
    from app.config import Settings

    db_file = tmp_path / "drafts_test.db"
    url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    manager = create_engine_and_session(Settings(database_url=url))
    eng, _factory = await manager.__aenter__()

    await create_all(eng)
    try:
        yield eng
    finally:
        await drop_all(eng)
        await manager.__aexit__(None, None, None)


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
        await sess.rollback()


@pytest_asyncio.fixture
async def case_factory(session: AsyncSession):  # noqa: ANN201
    """Создать дело. Позволяет тесту задать нужный номер."""
    from app.db.repositories import CaseRepository

    async def _make(number: str) -> Case:
        created = await CaseRepository(session).create(
            case_number=number,
            court_name="Демонстрационный районный суд",
            case_type="civil",
            category="договорные споры",
            is_synthetic=True,
        )
        await session.flush()
        return created

    return _make


# ── Служебные заглушки ────────────────────────────────────────────────


def _fake_generate(case_id: uuid.UUID):  # noqa: ANN202
    """Заменить генератор, чтобы тест не ждал реальную модель.

    Возвращается настоящий DraftResult с настоящими SectionDraft: если
    подменить и их, тест перестанет проверять контракт между генератором,
    сервисом и проверкой — а именно он здесь и ломается.
    """
    from app.agents.draft_agent import DraftResult, SectionDraft

    async def _generate(pack, template, *, provider=None, settings=None, **kwargs):  # noqa: ANN001, ANN202
        return DraftResult(
            case_number=pack.case_number or str(case_id),
            provider="test",
            model="test-model",
            total_latency_ms=42,
            sections=[
                SectionDraft(
                    key="introductory",
                    name="Вводная часть",
                    text="Дело № DEMO-001 рассмотрено в демонстрационном суде.",
                    is_mock=False,
                    citations=["01_claim.txt"],
                    latency_ms=10,
                ),
                SectionDraft(
                    key="operative",
                    name="РЕШИЛ",
                    # Раздел помечен как требующий проверки: так проверка
                    # должна увидеть, что подтверждения недостаточно.
                    text="Взыскать 150000 рублей. ТРЕБУЕТ ПРОВЕРКИ СПЕЦИАЛИСТОМ",
                    is_mock=False,
                    needs_review=True,
                    citations=["01_claim.txt"],
                    latency_ms=12,
                ),
            ],
        )

    return _generate


@pytest.fixture
async def prepared_case(session: AsyncSession, case_factory) -> uuid.UUID:  # noqa: ANN001
    """Дело с тремя документами — достаточно для пакета доказательств."""
    from app.db.repositories import DocumentRepository

    case = await case_factory("SVC-001")
    repo = DocumentRepository(session)

    texts = [
        ("01_claim.txt", "CLAIM",
         "Исковое заявление о взыскании задолженности 150000 руб. "
         "В силу статьи 309 ГК РФ обязательства должны быть исполнены надлежащим образом."),
        ("03_contract.txt", "CONTRACT",
         "ДОГОВОР ПОСТАВКИ № Д-2026/001 от 15 мая 2026 года. "
         "Истец: Образцов Пётр Петрович. Ответчик: Фирма «Вымышленная Строй». "
         "Ответчик обязался передать товар на 150000 руб."),
        ("04_receipt.txt", "RECEIPT",
         "ПЛАТЁЖНОЕ ПОРУЧЕНИЕ. Сумма перевода: 120 000,00 руб. "
         "Назначение платежа: частичное погашение задолженности."),
    ]

    for filename, doc_type, text in texts:
        await repo.add(
            _document(case.id, filename, doc_type, text)
        )
    await session.flush()
    return case.id


def _document(case_id: uuid.UUID, filename: str, doc_type: str, text: str):  # noqa: ANN202
    import hashlib

    from app.models.entities import CaseDocument

    return CaseDocument(
        case_id=case_id,
        filename=filename,
        document_type=doc_type,
        mime_type="text/plain",
        page_count=1,
        text=text,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


# ── Пакет доказательств ───────────────────────────────────────────────


class TestEvidencePackPersistence:
    async def test_pack_is_saved(self, session: AsyncSession, prepared_case: uuid.UUID) -> None:
        """Пакет доказательств должен попасть в базу."""
        service = DraftService(session)
        pack = await service.build_pack(prepared_case)
        assert pack.items, "пакет должен содержать факты"

        saved = await service.save_pack(prepared_case, pack)
        assert saved == len(pack.items)

        stored = await service.list_evidence(prepared_case)
        assert len(stored) == saved

    async def test_saved_item_keeps_source(self, session: AsyncSession, prepared_case: uuid.UUID) -> None:
        """Факт без источника в базу не попадает.

        Это ключевое требование: пакет — основа для объяснения, откуда
        взялся каждый фрагмент проекта.
        """
        service = DraftService(session)
        pack = await service.build_pack(prepared_case)
        await service.save_pack(prepared_case, pack)

        for item in await service.list_evidence(prepared_case):
            assert item.content, "факт без текста недопустим"
            assert item.source.get("document"), "факт без источника недопустим"
            assert item.category

    async def test_resave_replaces_old_pack(self, session: AsyncSession, prepared_case: uuid.UUID) -> None:
        """Повторная сборка не оставляет смеси старого и нового пакета."""
        service = DraftService(session)
        pack = await service.build_pack(prepared_case)
        await service.save_pack(prepared_case, pack)
        first = len(await service.list_evidence(prepared_case))

        await service.save_pack(prepared_case, pack)
        second = len(await service.list_evidence(prepared_case))

        assert second == first, "повторная запись должна заменять, а не дополнять"

    async def test_empty_case_is_rejected(self, session: AsyncSession, case_factory) -> None:  # noqa: ANN001
        """По пустому делу проект составлять нельзя."""
        case = await case_factory("EMPTY-001")
        service = DraftService(session)

        with pytest.raises(CaseNotReadyError):
            await service.build_pack(case.id)

    async def test_unknown_case_is_rejected(self, session: AsyncSession) -> None:
        service = DraftService(session)
        with pytest.raises(LookupError):
            await service.build_pack(uuid.uuid4())


# ── Фоновая генерация через настоящий сервис ─────────────────────────


class TestDraftGeneration:
    async def test_draft_and_version_saved(self, session: AsyncSession, prepared_case: uuid.UUID, monkeypatch) -> None:  # noqa: ANN001
        """Проект, версия и проверка должны сохраниться в базу."""
        from app.services import draft_service as module

        monkeypatch.setattr(
            module, "generate_draft",
            _fake_generate(prepared_case),
        )

        service = DraftService(session)
        draft, version, report, pack = await service.generate(prepared_case)

        assert draft.id is not None
        assert version.version == 1
        assert version.author == "ai"
        assert "Взыскать" in version.text
        assert report.sections_checked == 2
        assert pack.items

    async def test_evidence_saved_alongside_draft(self, session: AsyncSession, prepared_case: uuid.UUID, monkeypatch) -> None:  # noqa: ANN001
        """Пакет сохраняется вместе с проектом, а не после него.

        Иначе к сохранённому проекту нельзя было бы объяснить,
        откуда взялись факты.
        """
        from app.services import draft_service as module

        monkeypatch.setattr(
            module, "generate_draft", _fake_generate(prepared_case)
        )

        service = DraftService(session)
        await service.generate(prepared_case)

        assert await service.list_evidence(prepared_case), "пакет должен сохраниться"

    async def test_second_generation_creates_version_two(
        self, session: AsyncSession, prepared_case: uuid.UUID, monkeypatch
    ) -> None:  # noqa: ANN001
        """Повторная генерация добавляет версию, а не заменяет проект."""
        from app.services import draft_service as module

        monkeypatch.setattr(
            module, "generate_draft", _fake_generate(prepared_case)
        )

        service = DraftService(session)
        first, v1, _, _ = await service.generate(prepared_case)
        second, v2, _, _ = await service.generate(prepared_case)

        assert first.id == second.id, "проект должен остаться тем же"
        assert v1.version == 1
        assert v2.version == 2

        versions = await service.list_versions(first.id)
        assert [v.version for v in versions] == [1, 2]

    async def test_versions_kept_in_full(self, session: AsyncSession, prepared_case: uuid.UUID, monkeypatch) -> None:  # noqa: ANN001
        """Первая версия не затирается второй.

        Требование п.31: хранится полный текст каждой редакции, а не
        только разметка изменений.
        """
        from app.services import draft_service as module

        monkeypatch.setattr(
            module, "generate_draft", _fake_generate(prepared_case)
        )

        service = DraftService(session)
        draft, v1, _, _ = await service.generate(prepared_case)

        # Правка человека целиком, а не «изменённые места».
        await service.add_manual_version(
            draft.id,
            text="ПОЛНЫЙ ТЕКСТ ПРАВКИ СУДЬИ",
            author="judge",
            comment="исправлена резолютивная часть",
        )

        versions = await service.list_versions(draft.id)
        assert len(versions) == 2
        assert versions[0].text == v1.text, "первая версия не должна измениться"
        assert versions[1].text == "ПОЛНЫЙ ТЕКСТ ПРАВКИ СУДЬИ"
        assert versions[1].author == "judge"

    async def test_manual_edit_keeps_sections_and_sources(  # noqa: ANN001
        self, session: AsyncSession, prepared_case: uuid.UUID, monkeypatch
    ) -> None:
        """Правка судьи не стирает разделы и ссылки на источники.

        Дефект найден на рабочей базе при подготовке демонстрации:
        во всех проектах, где судья правил текст, последняя версия
        хранилась с пустым списком разделов. Интерфейс показывал
        «разделов 0 · источников 0» — ссылки на материалы дела
        исчезали именно тогда, когда проект брали в работу.
        """
        from app.services import draft_service as module

        monkeypatch.setattr(
            module, "generate_draft", _fake_generate(prepared_case)
        )

        service = DraftService(session)
        draft, v1, _, _ = await service.generate(prepared_case)
        assert v1.citations, "у сформированного проекта должны быть разделы"

        edited = v1.text  # судья правит формулировку, не переписывая акт
        await service.add_manual_version(
            draft.id,
            text=edited,
            author="judge",
            comment="исправлена сумма",
        )

        versions = await service.list_versions(draft.id)
        citations = versions[-1].citations
        assert isinstance(citations, list) and citations, (
            "правка судьи сохранила проект без разделов — "
            "интерфейс покажет «разделов 0»"
        )
        keys = [section["key"] for section in citations]
        assert keys == ["introductory", "operative"]
        sources = {
            str(c) for section in citations for c in section["citations"]
        }
        assert sources == {"01_claim.txt"}, "источники потеряны"

    async def test_author_must_be_human(self, session: AsyncSession, prepared_case: uuid.UUID, monkeypatch) -> None:  # noqa: ANN001
        """Правку может внести только человек, не машина.

        Иначе в истории проекта появились бы записи от модели,
        и «кто подписал акт» перестало бы иметь смысл.
        """
        from app.services import draft_service as module

        monkeypatch.setattr(
            module, "generate_draft", _fake_generate(prepared_case)
        )

        service = DraftService(session)
        draft, _, _, _ = await service.generate(prepared_case)

        with pytest.raises(ValueError):
            await service.add_manual_version(
                draft.id, text="текст", author="robot"
            )


# ── Фоновые задачи ────────────────────────────────────────────────────


class TestJobRegistry:
    async def test_lifecycle(self) -> None:
        registry = JobRegistry()
        case_id = uuid.uuid4()

        job = await registry.create(case_id)
        assert job.state is JobState.PENDING

        await registry.update(job.id, state=JobState.RUNNING)
        assert (await registry.get(job.id)).state is JobState.RUNNING

        await registry.update(job.id, state=JobState.DONE)
        assert (await registry.get(job.id)).state is JobState.DONE

    async def test_missing_job_returns_none(self) -> None:
        registry = JobRegistry()
        assert await registry.get(uuid.uuid4()) is None

    async def test_capacity_bounded(self) -> None:
        """Реестр не должен расти бесконечно.

        Долгоживущий сервер иначе со временем съел бы память на всех
        когда-либо выполненных задачах.
        """
        registry = JobRegistry(capacity=5)

        for _ in range(12):
            await registry.create(uuid.uuid4())

        assert registry.count() == 5

    async def test_dict_has_no_raw_objects(self) -> None:
        """В ответе клиенту не должно быть сырых исключений."""
        registry = JobRegistry()
        job = await registry.create(uuid.uuid4())
        await registry.update(job.id, error="ValueError: секретный текст")

        payload = (await registry.get(job.id)).to_dict()
        assert isinstance(payload["job_id"], str)
        assert payload["error"] == "ValueError: секретный текст"


# ── Эндпоинты ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def api_client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    """Клиент с подключённой временной БД.

    Без подключения все доменные эндпоинты отвечают 503 — это правильное
    поведение, но проверять его нужно отдельным тестом, а не случайно
    получать во всех остальных.
    """
    from app.config import AppEnv
    from app.db.bootstrap import ensure_schema
    from app.services.case_service import db

    settings = Settings(
        app_env=AppEnv.DEVELOPMENT,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'api.db').as_posix()}",
        storage_dir=tmp_path / "storage",
        documents_dir=tmp_path / "documents",
        demo_dir=tmp_path / "demo",
        # Шаблоны и законы — настоящие данные репозитория, БД и хранилища —
        # временные. Пустой каталог шаблонов ронял load_template, пустой
        # каталог законов отключал бы законодательные проверки верификации:
        # тест обязан идти по реальному пути, а не по лазейке.
        laws_dir=_REAL_LAWS_DIR,
        # Провайдеры задаются явно, а не берутся из env-файлов машины:
        # с backend/.env контрактные тесты молча уходили в реальный ollama
        # (3 минуты на класс и зависимость от запущенного сервера). Это
        # unit-тесты контракта — MOCK; реальный инференс проверяется
        # отдельным прогоном (REAL-01), а не этими тестами.
        llm_provider=LLMProviderName.MOCK,
        embedding_provider=EmbeddingProviderName.MOCK,
    )

    assert await db.connect(settings), "тесту нужна рабочая БД"
    await ensure_schema()

    app = create_app(settings)
    # raise_app_exceptions=False: неперехваченная ошибка обязана стать
    # HTTP-500 из общего обработчика main.py, а не исключением в тесте —
    # иначе нельзя проверить контракт ошибки (без traceback в ответе).
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        await db.disconnect()


class TestDraftEndpoints:
    async def test_get_draft_returns_none_without_draft(self, api_client: AsyncClient) -> None:
        """По существующему делу без проекта — 200 и null, а не 404.

        Клиент отличает «проекта ещё нет» (200 + null) от «дела нет»
        (404 — см. TestUnknownCaseContract).
        """
        case_id = await _make_case_with_documents(api_client)
        response = await api_client.get(f"/api/cases/{case_id}/draft")
        assert response.status_code == 200
        assert response.json() is None

    async def test_versions_without_draft_is_404(self, api_client: AsyncClient) -> None:
        """Список версий без проекта — явная ошибка: просить нечего."""
        case_id = await _make_case_with_documents(api_client)
        response = await api_client.get(f"/api/cases/{case_id}/draft/versions")
        assert response.status_code == 404
        assert "нет проекта" in response.json()["detail"]

    async def test_unknown_job_is_404(self, api_client: AsyncClient) -> None:
        response = await api_client.get(f"/api/jobs/{uuid.uuid4()}")
        assert response.status_code == 404
        assert "перезапуск" in response.json()["detail"].lower()

    async def test_missing_case_is_404(self, api_client: AsyncClient) -> None:
        response = await api_client.post(
            f"/api/cases/{uuid.uuid4()}/draft", json={"actor": "judge"}
        )
        assert response.status_code in {404, 503}

    async def test_edit_rejects_wrong_author(self, api_client: AsyncClient) -> None:
        """Автора правки задаёт клиент, и значение проверяется."""
        response = await api_client.post(
            f"/api/drafts/{uuid.uuid4()}/versions",
            json={"text": "текст", "author": "robot"},
        )
        assert response.status_code in {404, 422}

    async def test_responses_carry_disclaimer(self) -> None:
        """Каждый ответ о проекте обязан напоминать, что это проект.

        Проверяется на схеме: еслиDisclaimer исчезнет из ответа,
        пользователь увидит текст без предостережения.
        """
        from app.schemas.draft import DraftJobOut, DraftListOut, DraftOut

        for schema in (DraftOut, DraftListOut, DraftJobOut):
            defaults = schema.model_fields["disclaimer"].default
            assert defaults and "проект" in defaults.lower()
            assert schema.model_fields["is_draft"].default is True


# ── Регрессии: протухшие серверные поля ────────────────────────────────


class _BrokenProvider:
    """Провайдер, который всегда падает: имитация отказа LLM."""

    name = "broken"

    async def generate(self, request):  # noqa: ANN001, ANN202
        raise RuntimeError("LLM недоступен: имитация обрыва соединения")

    async def generate_json(self, request):  # noqa: ANN001, ANN202
        raise RuntimeError("LLM недоступен: имитация обрыва соединения")


async def _make_case_with_documents(client: AsyncClient) -> str:
    """Дело с материалами через API — тот же путь, что у интерфейса."""
    case = (
        await client.post(
            "/api/cases",
            json={
                "case_number": "REF-001",
                "court_name": "Тестовый районный суд",
                "case_type": "civil",
                "category": "договорные споры",
                "is_synthetic": True,
            },
        )
    ).json()
    documents = [
        (
            "01_claim.txt",
            "CLAIM",
            "Исковое заявление о взыскании задолженности 150000 руб. "
            "В силу статьи 309 ГК РФ обязательства должны быть исполнены надлежащим образом.",
        ),
        (
            "04_receipt.txt",
            "RECEIPT",
            "ПЛАТЁЖНОЕ ПОРУЧЕНИЕ. Сумма перевода: 120 000,00 руб. "
            "Назначение платежа: частичное погашение задолженности.",
        ),
    ]
    for filename, doc_type, text in documents:
        response = await client.post(
            f"/api/cases/{case['id']}/documents",
            json={"filename": filename, "document_type": doc_type, "text": text},
        )
        assert response.status_code == 201, response.text
    return str(case["id"])


async def _generate_sync(client: AsyncClient, case_id: str) -> dict:
    """Синхронная генерация: контракт — 202 и тело проекта."""
    response = await client.post(
        f"/api/cases/{case_id}/draft?wait=true", json={"actor": "judge"}
    )
    assert response.status_code == 202, response.text
    return response.json()


async def _wait_job(client: AsyncClient, job_id: str) -> dict:
    """Дождаться завершения фоновой задачи (в MOCK-режиме это быстро)."""
    for _ in range(200):
        response = await client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["state"] in ("DONE", "FAILED"):
            return job
        await asyncio.sleep(0.05)
    raise AssertionError("фоновая задача не завершилась за отведённое время")


def _db_counts(db_file: Path) -> tuple[int, int]:
    """Число проектов и версий напрямую в файле БД (не через API)."""
    import sqlite3

    conn = sqlite3.connect(db_file.as_posix())
    try:
        drafts = conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]
        versions = conn.execute("SELECT COUNT(*) FROM draft_versions").fetchone()[0]
    finally:
        conn.close()
    return int(drafts), int(versions)


class TestRefreshAfterUpdate:
    """Регрессия MissingGreenlet: updated_at протухает после UPDATE.

    updated_at в TimestampMixin — server_default + onupdate=func.now():
    после flush UPDATE SQLAlchemy помечает атрибут протухшим, а чтение
    вне зелёной нити роняет запрос (sqlalchemy.exc.MissingGreenlet).
    Инцидент: POST /draft?wait=true отвечал 500 и — из-за отката
    транзакции в session_scope — молча не сохранял проект.

    Генерация здесь идёт в MOCK-режиме приложения: это тесты контракта
    HTTP API и транзакций, а не реального качества LLM. Реальный прогон
    ollama проверяется отдельно (см. REAL01_REPAIR_REPORT.md).
    """

    async def test_sync_generation_persists_draft_and_version(
        self, api_client: AsyncClient, tmp_path: Path
    ) -> None:
        """wait=true: 202, проект и версия реально в БД, повторное чтение работает."""
        case_id = await _make_case_with_documents(api_client)
        draft = await _generate_sync(api_client, case_id)

        assert draft["id"]
        assert draft["case_id"] == case_id
        assert draft["status"] == "DRAFT"
        assert draft["template_name"] == "civil_judgment"
        assert draft["current_version"] == 1
        assert draft["sections"], "у проекта должны быть разделы"
        # MOCK-режим обязан быть виден в данных, а не выдаваться за реальный.
        assert any(section["is_mock"] for section in draft["sections"])
        assert draft["created_at"] and draft["updated_at"]
        assert draft["verification"] is not None
        assert draft["verification"]["sections_checked"] >= 1

        # Повторное чтение — новая сессия: данные действительно закоммичены.
        reread = (await api_client.get(f"/api/cases/{case_id}/draft")).json()
        assert reread["id"] == draft["id"]
        assert reread["updated_at"] == draft["updated_at"]
        assert reread["text"] == draft["text"]

        versions = (await api_client.get(f"/api/cases/{case_id}/draft/versions")).json()
        assert len(versions["versions"]) == 1
        assert versions["versions"][0]["text"] == draft["text"]

        assert _db_counts(tmp_path / "api.db") == (1, 1)

    async def test_status_patch_refreshes_updated_at(self, api_client: AsyncClient) -> None:
        """PATCH статуса: свежий updated_at в ответе и в повторном чтении."""
        case_id = await _make_case_with_documents(api_client)
        draft = await _generate_sync(api_client, case_id)

        response = await api_client.patch(
            f"/api/drafts/{draft['id']}/status",
            json={"status": "UNDER_REVIEW"},
        )
        assert response.status_code == 200, response.text
        updated = response.json()
        assert updated["id"] == draft["id"]
        assert updated["status"] == "UNDER_REVIEW"
        assert updated["updated_at"]
        assert updated["updated_at"] >= draft["updated_at"]

        reread = (await api_client.get(f"/api/cases/{case_id}/draft")).json()
        assert reread["status"] == "UNDER_REVIEW"
        assert reread["updated_at"] == updated["updated_at"]

    async def test_llm_section_failure_marks_honest_placeholder(
        self, api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Отказ LLM по ходу генерации: проект собирается, секции честно помечены.

        Контракт draft_agent: упавшая секция получает текст
        «Раздел не подготовлен...» с needs_review — не пустой текст
        и не успешный вид.
        """
        import app.llm as llm_module

        monkeypatch.setattr(
            llm_module,
            "build_llm_provider",
            lambda cfg=None: _BrokenProvider(),
        )

        case_id = await _make_case_with_documents(api_client)
        draft = await _generate_sync(api_client, case_id)

        assert draft["sections"]
        failed = [s for s in draft["sections"] if "Раздел не подготовлен" in s["text"]]
        assert failed, "упавшие секции должны быть помечены честным текстом"
        assert all(section["needs_review"] for section in failed)

        # Правки резолютивной строятся правилами и не требуют модели —
        # они обязаны присутствовать даже при неработающей LLM.
        assert any(s["key"] == "operative" for s in draft["sections"])
        assert _db_counts(tmp_path / "api.db") == (1, 1)

    async def test_sync_generation_failure_rolls_back(
        self, api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Сбой генерации: 500 без traceback и без частичных данных."""
        from app.services import draft_service as draft_service_module

        def _boom(*args, **kwargs):  # noqa: ANN001, ANN020
            raise RuntimeError("LLM недоступен: имитация обрыва соединения")

        monkeypatch.setattr(draft_service_module, "generate_draft", _boom)
        case_id = await _make_case_with_documents(api_client)

        response = await api_client.post(
            f"/api/cases/{case_id}/draft?wait=true", json={"actor": "judge"}
        )
        assert response.status_code == 500, response.text
        body = response.text
        # Контракт ошибки: JSON с detail, без traceback и без текста исключения.
        assert response.json()["detail"]
        assert "Traceback" not in body
        assert "LLM недоступен" not in body
        assert "имитация" not in body

        # Транзакция откатилась: ни проекта, ни версий.
        assert (await api_client.get(f"/api/cases/{case_id}/draft")).json() is None
        versions = await api_client.get(f"/api/cases/{case_id}/draft/versions")
        assert versions.status_code == 404
        assert _db_counts(tmp_path / "api.db") == (0, 0)

    async def test_async_job_completes_and_saves_draft(self, api_client: AsyncClient) -> None:
        """Фоновый режим: задача уходит в DONE, проект сохранён и читается."""
        case_id = await _make_case_with_documents(api_client)
        response = await api_client.post(f"/api/cases/{case_id}/draft", json={})
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]
        assert job_id

        job = await _wait_job(api_client, str(job_id))
        assert job["state"] == "DONE", job.get("error")
        assert job["draft_id"]
        assert job["version"] == 1
        assert job["error"] == ""

        draft = (await api_client.get(f"/api/cases/{case_id}/draft")).json()
        assert draft is not None
        assert draft["id"] == job["draft_id"]

    async def test_async_failure_marks_job_failed_without_partial_data(
        self, api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Фоновый сбой: задача FAILED с причиной, данных не появляется."""
        from app.services import draft_service as draft_service_module

        def _boom(*args, **kwargs):  # noqa: ANN001, ANN020
            raise RuntimeError("LLM недоступен: имитация обрыва соединения")

        monkeypatch.setattr(draft_service_module, "generate_draft", _boom)
        case_id = await _make_case_with_documents(api_client)

        response = await api_client.post(f"/api/cases/{case_id}/draft", json={})
        assert response.status_code == 202, response.text

        job = await _wait_job(api_client, str(response.json()["job_id"]))
        assert job["state"] == "FAILED"
        error = str(job["error"])
        assert "RuntimeError" in error
        assert "Traceback" not in error

        assert (await api_client.get(f"/api/cases/{case_id}/draft")).json() is None
        assert _db_counts(tmp_path / "api.db") == (0, 0)

    async def test_manual_edit_creates_second_version(self, api_client: AsyncClient, tmp_path: Path) -> None:
        """Правка человека: INSERT версии + UPDATE проекта, без протухания."""
        case_id = await _make_case_with_documents(api_client)
        draft = await _generate_sync(api_client, case_id)

        response = await api_client.post(
            f"/api/drafts/{draft['id']}/versions",
            json={
                "text": "РЕШИЛ: взыскать с ответчика 150000 руб.",
                "author": "judge",
                "comment": "уточнение резолютивной",
            },
        )
        assert response.status_code == 201, response.text
        version = response.json()
        assert version["version"] == 2
        assert version["created_at"]
        assert version["text"].startswith("РЕШИЛ")

        versions = (await api_client.get(f"/api/cases/{case_id}/draft/versions")).json()
        assert len(versions["versions"]) == 2
        assert versions["current_version"] == 2
        assert _db_counts(tmp_path / "api.db") == (1, 2)

    async def test_case_status_patch_returns_fresh_timestamp(
        self, api_client: AsyncClient
    ) -> None:
        """Дело: тот же класс серверных полей (refresh уже был в update_case)."""
        case_id = await _make_case_with_documents(api_client)
        response = await api_client.patch(
            f"/api/cases/{case_id}", json={"status": "PROCESSING"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "PROCESSING"
        assert body["updated_at"]

        reread = (await api_client.get(f"/api/cases/{case_id}")).json()
        assert reread["status"] == "PROCESSING"
        assert reread["updated_at"] == body["updated_at"]


class TestWaitResponseVerification:
    """Ответ wait=true показывает те же замечания, что сохранены в базе.

    Регрессия, пойманная на реальном прогоне REAL-01: свежий
    VerificationReport держит объекты Issue, а _draft_out фильтровал
    элементы проверкой isinstance(item, dict) — синхронный ответ
    отвечал «0 замечаний», когда в сохранённой записи их было 5.
    """

    async def test_issue_objects_survive_post_and_get(
        self, api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services import draft_service as draft_service_module
        from app.verification.engine import (
            Issue,
            IssueType,
            Severity,
            VerificationReport,
            VerificationStatus,
        )

        def _stub_verify(draft, pack, settings=None):  # noqa: ANN001, ANN202
            return VerificationReport(
                status=VerificationStatus.REVIEW_REQUIRED,
                issues=[
                    Issue(
                        type=IssueType.INCOMPLETE,
                        severity=Severity.HIGH,
                        text="раздел не сформирован",
                        reason="объект Issue должен дойти до ответа",
                        section="Мотивировочная часть",
                    )
                ],
                sections_checked=1,
                claims_checked=1,
                unsupported_claims=1,
            )

        monkeypatch.setattr(draft_service_module, "verify_draft", _stub_verify)

        case_id = await _make_case_with_documents(api_client)
        draft = await _generate_sync(api_client, case_id)

        assert draft["verification"] is not None
        issues = draft["verification"]["issues"]
        assert len(issues) == 1, "объекты Issue терялись в ответе wait=true"
        assert issues[0]["severity"] == "HIGH"
        assert issues[0]["reason"] == "объект Issue должен дойти до ответа"

        # Повторное чтение из базы — те же данные, что и в POST-ответе.
        reread = (await api_client.get(f"/api/cases/{case_id}/draft")).json()
        assert reread["verification"] is not None
        reread_reasons = [i["reason"] for i in reread["verification"]["issues"]]
        assert reread_reasons == ["объект Issue должен дойти до ответа"]


class TestUnknownCaseContract:
    """Неизвестное дело — 404, а не пустой ответ.

    Регрессия: GET /draft по несуществующему UUID возвращал 200 + null —
    «проекта ещё нет» нельзя было отличить от опечатки в идентификаторе.
    Для существующего дела без проекта null остаётся: на нём держится
    экран «Проект не сформирован».
    """

    async def test_case_scoped_draft_routes_404_for_unknown_case(
        self, api_client: AsyncClient
    ) -> None:
        unknown = "00000000-0000-0000-0000-000000000000"
        for path in (
            f"/api/cases/{unknown}/draft",
            f"/api/cases/{unknown}/draft/versions",
            f"/api/cases/{unknown}/evidence",
        ):
            response = await api_client.get(path)
            assert response.status_code == 404, f"{path}: {response.status_code}"
            assert response.json()["detail"] == "Дело не найдено"


# ── Согласованность перечислений ──────────────────────────────────────


class TestDuplicateDocumentAcrossCases:
    """Один и тот же документ в разных делах — не дубль.

    Типовой иск или типовой договор законно встречается во многих
    производствах. Раньше поиск по хэшу шёл по всей базе и выбрасывал
    MultipleResultsFound, из-за чего загрузка падала с 500.
    """

    async def test_same_document_allowed_in_two_cases(self, session: AsyncSession) -> None:
        from app.db.repositories import DocumentRepository

        repo = DocumentRepository(session)
        text = "Исковое заявление о взыскании 150000 руб."

        import hashlib

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

        first = await case_factory_one(session, "DUP-1")
        second = await case_factory_one(session, "DUP-2")

        await repo.add(_document(first.id, "иск.txt", "CLAIM", text))
        await repo.add(_document(second.id, "иск.txt", "CLAIM", text))
        await session.flush()

        # Поиск с ограничением по делу обязан найти свой документ
        # и не должен падать на наличии второго в другом деле.
        found_first = await repo.find_by_sha256(digest, case_id=first.id)
        found_second = await repo.find_by_sha256(digest, case_id=second.id)

        assert found_first is not None
        assert found_second is not None
        assert found_first.case_id == first.id
        assert found_second.case_id == second.id

    async def test_duplicate_within_case_is_found(self, session: AsyncSession) -> None:
        """Внутри одного дела дубль должен находиться — ради отказа 409."""
        import hashlib

        from app.db.repositories import DocumentRepository

        repo = DocumentRepository(session)
        text = "ПРОТОКОЛ"

        case = await case_factory_one(session, "DUP-3")
        await repo.add(_document(case.id, "p.txt", "PROTOCOL", text))
        await session.flush()

        found = await repo.find_by_sha256(
            hashlib.sha256(text.encode("utf-8")).hexdigest(), case_id=case.id
        )
        assert found is not None


async def case_factory_one(session: AsyncSession, number: str):  # noqa: ANN201
    from app.db.repositories import CaseRepository

    created = await CaseRepository(session).create(
        case_number=number,
        court_name="Тестовый суд",
        case_type="civil",
        is_synthetic=True,
    )
    await session.flush()
    return created


class TestStatusVocabulary:
    def test_draft_status_values(self) -> None:
        values = {s.value for s in DraftStatus}
        assert "DRAFT" in values

    def test_case_status_values(self) -> None:
        assert "NEW" in {s.value for s in CaseStatus}
