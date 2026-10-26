"""Эндпоинты дел и документов (п.42).

Зависимость get_session отдаёт транзакцию: коммит делает сервисный слой,
роутер только переводит HTTP в вызов домена и обратно.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Case, CaseDocument, CaseStatus, DocumentType, Participant
from app.schemas.case import (
    CaseCreate,
    CaseListOut,
    CaseOut,
    CaseUpdate,
    DocumentDetailOut,
    DocumentOut,
    DocumentRegister,
    ParticipantOut,
)
from app.services.case_service import CaseService, db, session_scope

router = APIRouter(prefix="/api/cases", tags=["cases"])

# Starlette отказался от старого имени; используем актуальное.
HTTP_422 = status.HTTP_422_UNPROCESSABLE_CONTENT

# Метки обезличивания исходных материалов («<ДАТА23>»): цифра внутри
# метки обязательна — иначе законный текст вида «<br>» или «<h2>»
# помечался бы как скрытые данные.
_PLACEHOLDER = re.compile(r"<[А-ЯЁA-Za-z]{2,12}\d{1,4}>")

# Форматы, которые умеет извлекать app.ingestion.extract.
_ALLOWED_SUFFIXES = frozenset({"txt", "md", "pdf", "docx", "html", "htm", "json"})


async def get_session() -> AsyncIterator[AsyncSession]:
    """Транзакция на один запрос.

    Если БД недоступна, отдаём 503 с понятным сообщением, а не 500:
    отсутствие БД — это состояние системы, а не ошибка кода.
    """
    if not db.available:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="База данных недоступна. Проверьте подключение в .env (DATABASE_URL).",
        )
    async with session_scope(db.factory) as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def _to_out(case: Case, session: AsyncSession) -> CaseOut:
    """Собрать ответ по делу.

    Счётчик документов берётся отдельным запросом намеренно: это один
    запрос на дело вместо N+1 по документам, а список дел ограничен
    параметром limit.
    """
    from app.db.repositories import DocumentRepository

    count = await DocumentRepository(session).count_for_case(case.id)

    return CaseOut(
        id=case.id,
        case_number=case.case_number,
        court_name=case.court_name,
        case_type=case.case_type,
        category=case.category,
        status=case.status,
        is_synthetic=case.is_synthetic,
        created_at=case.created_at,
        updated_at=case.updated_at,
        participants=[
            ParticipantOut(
                id=p.id, name=p.name, role=p.role, is_anonymized=p.is_anonymized
            )
            for p in case.participants
        ],
        documents=[],
        document_count=count,
    )


def _to_document_out(document: CaseDocument) -> DocumentOut:
    return DocumentOut(
        id=document.id,
        case_id=document.case_id,
        filename=document.filename,
        document_type=document.document_type,
        mime_type=document.mime_type,
        page_count=document.page_count,
        sha256=document.sha256,
        is_known_type=DocumentType.is_known(document.document_type),
        characters=len(document.text),
        has_placeholders=bool(_PLACEHOLDER.search(document.text)),
        created_at=document.created_at,
    )


@router.post("", response_model=CaseOut, status_code=status.HTTP_201_CREATED)
async def create_case(payload: CaseCreate, session: SessionDep) -> CaseOut:
    """POST /api/cases — создать дело (п.42)."""
    service = CaseService(session)
    try:
        case = await service.create_case(
            payload.case_number,
            payload.court_name,
            payload.case_type,
            category=payload.category,
            is_synthetic=payload.is_synthetic,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await session.refresh(case)
    return await _to_out(case, session)


@router.get("", response_model=CaseListOut)
async def list_cases(
    session: SessionDep,
    case_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CaseListOut:
    """GET /api/cases — список дел."""
    service = CaseService(session)

    if case_status and case_status not in {s.value for s in CaseStatus}:
        raise HTTPException(
            status_code=HTTP_422,
            detail=f"Неизвестный статус: {case_status}",
        )

    cases = await service.list_cases(status=case_status, limit=limit, offset=offset)
    total = await service.cases.count(status=case_status)
    return CaseListOut(
        items=[await _to_out(c, session) for c in cases],
        total=total,
    )


@router.get("/{case_id}", response_model=CaseOut)
async def get_case(case_id: uuid.UUID, session: SessionDep) -> CaseOut:
    """GET /api/cases/{id} — одно дело."""
    service = CaseService(session)
    case = await service.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дело не найдено")
    return await _to_out(case, session)


@router.patch("/{case_id}", response_model=CaseOut)
async def update_case(
    case_id: uuid.UUID, payload: CaseUpdate, session: SessionDep
) -> CaseOut:
    """PATCH /api/cases/{id} — сменить статус или категорию."""
    service = CaseService(session)

    if payload.status is not None:
        case = await service.set_status(case_id, payload.status.value)
    else:
        case = await service.get_case(case_id)
        if case is not None and payload.category is not None:
            case.category = payload.category
            await session.flush()

    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дело не найдено")

    await session.refresh(case)
    return await _to_out(case, session)


@router.get("/{case_id}/documents", response_model=list[DocumentOut])
async def list_documents(
    case_id: uuid.UUID,
    session: SessionDep,
    document_type: Annotated[str | None, Query(alias="type")] = None,
) -> list[DocumentOut]:
    """GET /api/cases/{id}/documents — документы дела (п.42)."""
    service = CaseService(session)
    if await service.get_case(case_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дело не найдено")

    documents = await service.documents.list_for_case(case_id, document_type=document_type)
    return [_to_document_out(d) for d in documents]


@router.get(
    "/{case_id}/documents/{document_id}", response_model=DocumentDetailOut
)
async def get_document(
    case_id: uuid.UUID, document_id: uuid.UUID, session: SessionDep
) -> DocumentDetailOut:
    """GET /api/cases/{id}/documents/{doc_id} — материал дела с текстом.

    Текст отдаётся отдельным запросом, а не списком: список материалов
    читают часто, полный текст нужен только когда на документ смотрят.
    Секретарь должен сверять цитату в проекте с исходным файлом —
    без этого цитате нечего противопоставить.
    """
    service = CaseService(session)
    if await service.get_case(case_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дело не найдено")

    document = await service.documents.get(document_id)
    if document is None or document.case_id != case_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Документ не найден"
        )

    base = _to_document_out(document)
    return DocumentDetailOut(
        **base.model_dump(),
        text=document.text,
        placeholders=list(dict.fromkeys(_PLACEHOLDER.findall(document.text)))[:50],
    )


@router.post(
    "/{case_id}/documents/upload",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    request: Request,
    case_id: uuid.UUID,
    file: Annotated[UploadFile, File(description="Файл материала дела")],
    session: SessionDep,
) -> DocumentOut:
    """POST /api/cases/{id}/documents/upload — загрузить файл (drag&drop).

    Каждая ошибка — это действие, а не код: интерфейс показывает
    сообщение дословно, поэтому текст писался для человека, которому
    всё это безразлично и непонятно.
    """
    service = CaseService(session)
    if await service.get_case(case_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дело не найдено")

    # Настройки приложения, а не глобальный кэш get_settings(): лимит
    # размера обязан совпадать с тем, что видит это приложение (в тестах
    # там лимит 0 МБ, в запуске — MAX_UPLOAD_MB из окружения).
    cfg = request.app.state.settings
    filename = (file.filename or "").strip()[:500] or "документ.txt"
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in _ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Файл «{filename}» не открывается: этот формат не поддерживается. "
                "Нужен PDF, Word (docx), текстовый файл (txt, md), HTML или JSON."
            ),
        )

    # На байт больше лимита — уже ошибка: читаем лимит+1 и проверяем.
    data = await file.read(cfg.max_upload_bytes + 1)
    if len(data) > cfg.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"Файл «{filename}» больше {cfg.max_upload_mb} МБ. "
                "Разделите его на части или уберите из него картинки."
            ),
        )
    if not data:
        raise HTTPException(
            status_code=HTTP_422, detail=f"Файл «{filename}» пустой — читать нечего."
        )

    # Дубль проверяется внутри дела: один и тот же документ законно
    # встречается в разных делах (см. DocumentRepository.find_by_sha256).
    digest = hashlib.sha256(data).hexdigest()
    existing = await service.documents.find_by_sha256(digest, case_id=case_id)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Такой файл уже есть в деле: {existing.filename}.",
        )

    from app.ingestion.pipeline import IngestionError, ingest_document

    try:
        result = await ingest_document(
            session, case_id, filename, data, settings=cfg, actor="ui"
        )
    except IngestionError as exc:
        # Текст не извлекся (скан без текстового слоя, битый файл) —
        # документ не создаётся, чтобы пустышка не выглядела загруженной.
        raise HTTPException(status_code=HTTP_422, detail=str(exc)) from exc

    document = await service.documents.get(result.document_id)
    if document is None:  # pragma: no cover - ingestion только что создал
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Документ записан, но не сохранился. Попробуйте ещё раз.",
        )
    return _to_document_out(document)


@router.post(
    "/{case_id}/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
)
async def register_document(
    case_id: uuid.UUID, payload: DocumentRegister, session: SessionDep
) -> DocumentOut:
    """POST /api/cases/{id}/documents — зарегистрировать документ.

    Фаза 5 заменит это загрузкой файла с извлечением текста. Пока
    принимаются метаданные и текст: этого достаточно, чтобы отработал
    путь «дело → документ → чанки» без бинарной загрузки.
    """
    service = CaseService(session)
    if await service.get_case(case_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дело не найдено")

    sha256 = hashlib.sha256(payload.text.encode("utf-8")).hexdigest()

    # Дубль ищется внутри дела: тот же документ в другом деле — не дубль,
    # а обычное дело (типовой иск встречается в десятках производств).
    existing = await service.documents.find_by_sha256(sha256, case_id=case_id)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Документ с таким содержимым уже загружен: {existing.filename}",
        )

    document = CaseDocument(
        case_id=case_id,
        filename=payload.filename,
        document_type=payload.document_type,
        mime_type=payload.mime_type,
        page_count=payload.page_count,
        text=payload.text,
        metadata_json=payload.metadata,
        sha256=sha256,
    )
    await service.documents.add(document)
    await service.audit.log(
        "document.registered",
        case_id=case_id,
        actor="api",
        document_id=str(document.id),
        document_type=payload.document_type,
        characters=len(payload.text),
    )
    await session.refresh(document)
    return _to_document_out(document)


@router.get("/{case_id}/participants", response_model=list[ParticipantOut])
async def list_participants(case_id: uuid.UUID, session: SessionDep) -> list[ParticipantOut]:
    """Участники дела (п.34)."""
    service = CaseService(session)
    case = await service.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дело не найдено")

    return [
        ParticipantOut(
            id=p.id, name=p.name, role=p.role, is_anonymized=p.is_anonymized
        )
        for p in case.participants
    ]


@router.post(
    "/{case_id}/participants",
    response_model=ParticipantOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_participant(
    case_id: uuid.UUID,
    payload: dict[str, str],
    session: SessionDep,
) -> ParticipantOut:
    """Добавить участника дела.

    Схема ответа берётся из query-параметров: полноценную схему входа
    добавим вместе с ingestion в Фазе 5, где участники извлекаются
    автоматически из текста документов.
    """
    service = CaseService(session)
    if await service.get_case(case_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дело не найдено")

    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=HTTP_422, detail="Не указано имя участника")

    participant = Participant(
        case_id=case_id,
        name=name,
        role=str(payload.get("role", "")),
        is_anonymized=bool(payload.get("is_anonymized", False)),
    )
    session.add(participant)
    await session.flush()

    await service.audit.log(
        "participant.added", case_id=case_id, actor="api", participant_id=str(participant.id)
    )
    await session.refresh(participant)
    return ParticipantOut(
        id=participant.id,
        name=participant.name,
        role=participant.role,
        is_anonymized=participant.is_anonymized,
    )
