"""Модели предметной области (п.46 ТЗ).

Осознанные решения:

* **UUID через SQLAlchemy `Uuid`, а не `String`.** На PostgreSQL это
  нативный uuid, на SQLite — CHAR(32). Одинаковый код на обеих БД.
* **Даты через `DateTime(timezone=True)`.** Даты действия нормы
  (п.16) должны сравниваться корректно, наивные даты в этом дают
  плавающие результаты в зависимости от настроек сервера.
* **Enum через `String`, а не нативный ENUM.** Нативный ENUM в PostgreSQL
  требует отдельной миграции для каждого нового значения и плохо
  переносится на SQLite. Строковые значения с проверкой на уровне
  Pydantic дают то же самое без миграционных ловушек.
* **JSON через `JSON`, не через `JSONB`.** JSONB — только PostgreSQL.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Базовый класс для всех моделей."""

    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


# ── Перечисления (п.9, п.11) ──────────────────────────────────────────────


class CaseStatus(StrEnum):
    """Статусы дела (п.9)."""

    NEW = "NEW"
    PROCESSING = "PROCESSING"
    DRAFT_READY = "DRAFT_READY"
    UNDER_REVIEW = "UNDER_REVIEW"
    CORRECTED = "CORRECTED"
    READY_FOR_APPROVAL = "READY_FOR_APPROVAL"
    ARCHIVED = "ARCHIVED"


class DocumentType(StrEnum):
    """Типы документов (п.11).

    Требование ТЗ: «типы должны быть расширяемыми». Поэтому хранится
    строка, а не закрытый ENUM — новый тип добавляется данными, а не
    миграцией.
    """

    CLAIM = "CLAIM"                      # исковое заявление
    RESPONSE = "RESPONSE"                # отзыв / возражения
    CONTRACT = "CONTRACT"                # договор
    RECEIPT = "RECEIPT"                  # платёжный документ
    ACT = "ACT"                          # акт
    EXPERT_OPINION = "EXPERT_OPINION"    # заключение эксперта
    EVIDENCE = "EVIDENCE"                # доказательство
    MOTION = "MOTION"                    # ходатайство
    PROTOCOL = "PROTOCOL"                # протокол
    OTHER = "OTHER"

    @classmethod
    def is_known(cls, value: str) -> bool:
        """Известен ли тип. Неизвестные значения допускаются (расширяемость)."""
        try:
            cls(value)
        except ValueError:
            return False
        return True


class DraftStatus(StrEnum):
    """Статус проекта судебного акта."""

    DRAFT = "DRAFT"
    UNDER_REVIEW = "UNDER_REVIEW"
    VERIFIED = "VERIFIED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    REGENERATED = "REGENERATED"


class VerificationStatus(StrEnum):
    """Статусы проверки (п.29).

    S105 на PASS — ложное срабатывание: это статус проверки, а не пароль.
    """

    PASS = "PASS"  # noqa: S105
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    BLOCKED = "BLOCKED"


class IssueType(StrEnum):
    """Типы замечаний проверки (п.28)."""

    UNSUPPORTED_CLAIM = "UNSUPPORTED_CLAIM"
    MISSING_SOURCE = "MISSING_SOURCE"
    INCORRECT_QUOTE = "INCORRECT_QUOTE"
    LAW_NOT_FOUND = "LAW_NOT_FOUND"
    LAW_NOT_IN_EFFECT = "LAW_NOT_IN_EFFECT"
    PRACTICE_NOT_FOUND = "PRACTICE_NOT_FOUND"
    CONTRADICTION = "CONTRADICTION"
    INCOMPLETE = "INCOMPLETE"


class Severity(StrEnum):
    """Серьёзность замечания."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class DraftAuthor(StrEnum):
    """Кто внёс версию (п.31)."""

    AI = "AI"
    SECRETARY = "SECRETARY"
    JUDGE = "JUDGE"


# ── Служебное ──────────────────────────────────────────────────────────────


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now() -> datetime:
    """Метка времени. timezone-aware — критично для сравнения дат."""
    return datetime.now(tz=None)


class TimestampMixin:
    """Поля created_at / updated_at для всех сущностей."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ── Case (п.9) ────────────────────────────────────────────────────────────


class Case(TimestampMixin, Base):
    """Судебное дело."""

    __tablename__ = "cases"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    case_number: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    court_name: Mapped[str] = mapped_column(String(300), nullable=False)
    case_type: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=CaseStatus.NEW.value, index=True
    )
    # Признак синтетических данных (п.40). Обязателен: демо-данные не должны
    # выглядеть как реальное дело.
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    documents: Mapped[list[CaseDocument]] = relationship(
        back_populates="case", cascade="all, delete-orphan", lazy="selectin"
    )
    participants: Mapped[list[Participant]] = relationship(
        back_populates="case", cascade="all, delete-orphan", lazy="selectin"
    )
    drafts: Mapped[list[Draft]] = relationship(
        back_populates="case", cascade="all, delete-orphan", lazy="selectin"
    )
    analysis: Mapped[CaseAnalysis | None] = relationship(
        back_populates="case", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (Index("ix_cases_number_court", "case_number", "court_name"),)

    def __repr__(self) -> str:
        return f"<Case {self.case_number} status={self.status}>"


class Participant(TimestampMixin, Base):
    """Участник дела."""

    __tablename__ = "participants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    case_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    # Роль: истец, ответчик, представитель, третье лицо, судья.
    role: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    # Обезличено ли имя. Исходное имя не хранится после обезличивания (п.45).
    is_anonymized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    case: Mapped[Case] = relationship(back_populates="participants")

    def __repr__(self) -> str:
        return f"<Participant {self.name} role={self.role}>"


# ── Документы (п.10, п.12) ───────────────────────────────────────────────


class CaseDocument(TimestampMixin, Base):
    """Документ дела с извлечённым текстом."""

    __tablename__ = "case_documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    case_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    # Строка, не ENUM: новые типы добавляются данными (п.11).
    document_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default=DocumentType.OTHER.value, index=True
    )
    mime_type: Mapped[str] = mapped_column(String(150), nullable=False, default="")
    page_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Текст хранится целиком для поиска и отображения источника.
    # Для очень больших файлов текст выносится в ФС — см. Фазу 5.
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict, nullable=False
    )
    # SHA256 для защиты от дублей и для проверки неизменности.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    case: Mapped[Case] = relationship(back_populates="documents")
    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (Index("ix_documents_case_type", "case_id", "document_type"),)

    @property
    def text_source(self) -> str:
        """Откуда текст: `native` (слой PDF) или `ocr` (распознан).

        Интерфейс показывает это в списке материалов: текст, полученный
        распознаванием, может содержать ошибки, и судья должен это знать
        до того, как увидит цитату из него.
        """
        if isinstance(self.metadata_json, dict):
            return str(self.metadata_json.get("text_source", "native"))
        return "native"

    def __repr__(self) -> str:
        return f"<CaseDocument {self.filename} type={self.document_type}>"


class DocumentChunk(TimestampMixin, Base):
    """Фрагмент документа с provenance (п.12).

    Provenance — не украшение: без него нельзя показать «источник
    утверждения» в интерфейсе, а это ключевое требование проекта.
    """

    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("case_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    case_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)

    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    # ── Provenance (п.12) ────────────────────────────────────────────
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paragraph: Mapped[int | None] = mapped_column(Integer, nullable=True)
    character_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    character_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Якорь для цитирования: как найти источник в интерфейсе.
    anchor: Mapped[str] = mapped_column(String(300), nullable=False, default="")

    document: Mapped[CaseDocument] = relationship(back_populates="chunks")

    __table_args__ = (
        Index("ix_chunks_doc_index", "document_id", "chunk_index"),
        Index("ix_chunks_case_id", "case_id"),
    )

    def __repr__(self) -> str:
        return f"<DocumentChunk {self.chunk_index} page={self.page}>"


# ── Анализ дела (п.21) ────────────────────────────────────────────────────


class CaseAnalysis(TimestampMixin, Base):
    """Результат анализа дела."""

    __tablename__ = "case_analyses"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    case_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    facts: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    claims: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    objections: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    evidence: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    procedural_history: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    disputed_facts: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    established_facts: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    unresolved_questions: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)

    # Признак, что анализ получен из заглушки. Проверка обязана знать.
    is_mock: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    case: Mapped[Case] = relationship(back_populates="analysis")

    def __repr__(self) -> str:
        return f"<CaseAnalysis facts={len(self.facts)} claims={len(self.claims)}>"


class EvidenceItem(TimestampMixin, Base):
    """Элемент Evidence Pack (п.26)."""

    __tablename__ = "evidence_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    case_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Источник: JSON со ссылкой на документ/чанк.
    source: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    # Уверенность 0..1
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Требует ли проверки человеком
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (Index("ix_evidence_case_category", "case_id", "category"),)

    def __repr__(self) -> str:
        return f"<EvidenceItem {self.category} review={self.requires_review}>"


# ── Проект акта и версии (п.31) ───────────────────────────────────────────


class Draft(TimestampMixin, Base):
    """Проект судебного акта."""

    __tablename__ = "drafts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    case_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=DraftStatus.DRAFT.value, index=True
    )
    template_name: Mapped[str] = mapped_column(
        String(100), nullable=False, default="civil_judgment"
    )
    current_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    case: Mapped[Case] = relationship(back_populates="drafts")
    versions: Mapped[list[DraftVersion]] = relationship(
        back_populates="draft", cascade="all, delete-orphan", lazy="selectin"
    )
    verifications: Mapped[list[Verification]] = relationship(
        back_populates="draft", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<Draft v{self.current_version} status={self.status}>"


class DraftVersion(TimestampMixin, Base):
    """Версия проекта (п.31).

    Хранится полный текст каждой версии, а не только diff: юридический
    акт нужно уметь показать в том виде, в каком он был принят.
    """

    __tablename__ = "draft_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    draft_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("drafts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str] = mapped_column(
        String(30), nullable=False, default=DraftAuthor.AI.value
    )
    comment: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Ссылки на источники, использованные в этой версии.
    citations: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)

    draft: Mapped[Draft] = relationship(back_populates="versions")

    __table_args__ = (
        Index("ix_versions_draft_version", "draft_id", "version", unique=True),
    )

    def __repr__(self) -> str:
        return f"<DraftVersion v{self.version} author={self.author}>"


class Verification(TimestampMixin, Base):
    """Результат проверки проекта (п.28, п.29)."""

    __tablename__ = "verifications"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    draft_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("drafts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    draft_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=VerificationStatus.REVIEW_REQUIRED.value
    )
    issues: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    # Сколько утверждений проверено и сколько из них без источника.
    claims_checked: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unsupported_claims: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Сколько разделов проекта было проверено. Отдельно от claims_checked:
    # при переработке проверки одно утверждение может относиться к разделу,
    # и без этого поля нельзя сказать, охват был полным или частичным.
    sections_checked: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    draft: Mapped[Draft] = relationship(back_populates="verifications")

    def __repr__(self) -> str:
        return f"<Verification v{self.draft_version} status={self.status}>"


# ── Знание (п.15–п.17) ───────────────────────────────────────────────────


class LawDocument(TimestampMixin, Base):
    """Нормативный акт (п.16).

    effective_from / effective_to обязательны: ссылка на норму без указания
    редакции — дефект, который ловит Verification Engine (п.28).
    """

    __tablename__ = "law_documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    number: Mapped[str] = mapped_column(String(100), nullable=False, default="", index=True)
    article: Mapped[str] = mapped_column(String(50), nullable=False, default="", index=True)
    part: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    paragraph: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    text: Mapped[str] = mapped_column(Text, nullable=False)

    effective_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # NULL = действует по сейчас.
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    version: Mapped[str] = mapped_column(String(50), nullable=False, default="")

    def __repr__(self) -> str:
        return f"<LawDocument {self.number} art={self.article} from={self.effective_from}>"


class CourtDecision(TimestampMixin, Base):
    """Судебная практика (п.17)."""

    __tablename__ = "court_decisions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    court: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    case_number: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    decision_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(100), nullable=False, default="", index=True)
    facts: Mapped[str] = mapped_column(Text, default="", nullable=False)
    decision: Mapped[str] = mapped_column(Text, default="", nullable=False)
    legal_position: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    def __repr__(self) -> str:
        return f"<CourtDecision {self.court} {self.decision_date}>"


class JudgmentTemplate(TimestampMixin, Base):
    """Шаблон судебного акта (п.18)."""

    __tablename__ = "judgment_templates"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    # Структура шаблона конфигурируема (п.18), поэтому хранится данными.
    sections: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    def __repr__(self) -> str:
        return f"<JudgmentTemplate {self.name}>"


# ── Обратная связь и аудит (п.32, п.46) ────────────────────────────────────


class FeedbackSample(TimestampMixin, Base):
    """Обезличенный пример правки (п.32).

    В MVP только накапливается. Автообучение не выполняется: п.32 прямо
    запрещает обучать модель после каждого изменения.
    """

    __tablename__ = "feedback_samples"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    ai_text: Mapped[str] = mapped_column(Text, nullable=False)
    human_text: Mapped[str] = mapped_column(Text, nullable=False)
    comment: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Только обезличенные данные (п.45).
    is_anonymized: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    def __repr__(self) -> str:
        return f"<FeedbackSample anonymized={self.is_anonymized}>"


class AuditEntry(TimestampMixin, Base):
    """Запись аудита (п.46).

    Первичный ключ — автоинкрементный Integer, а не UUID, сознательно:
    журнал аудита обязан иметь надёжный порядок, а `func.now()` в SQLite
    даёт точность до секунды, из-за чего две записи в одной транзакции
    получают одинаковое время и порядок становится произвольным.
    Порядковый номер решает это на уровне схемы, а не на уровне запроса.
    """

    __tablename__ = "audit_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("cases.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(100), nullable=False, default="system")
    # Содержимое произвольное, но БЕЗ текста документов (безопасность).
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    def __repr__(self) -> str:
        return f"<AuditEntry #{self.id} {self.action} by={self.actor}>"


# ── Отзывы пользователей (обучение и улучшение) ────────────────────────────


class FeedbackVerdict(StrEnum):
    """Итоговая оценка проекта человеком.

    Три состояния, а не свободный текст: по ним можно считать статистику
    и строить выборку для дообучения. Свободное мнение остаётся в comment.
    """

    ACCEPTED = "ACCEPTED"        # проект можно брать в работу
    NEEDS_WORK = "NEEDS_WORK"    # нужна доработка
    REJECTED = "REJECTED"        # непригодно


class FeedbackAuthorRole(StrEnum):
    """Кто оставил отзыв. Для вуза важны студенты и преподаватели."""

    STUDENT = "student"
    TEACHER = "teacher"
    JUDGE = "judge"
    SECRETARY = "secretary"
    OTHER = "other"


class Feedback(TimestampMixin, Base):
    """Отзыв о проекте судебного акта.

    Зачем отдельная таблица, а не FeedbackSample: для дообучения нужна
    пара «что показала модель» → «как оценил человек», привязанная к делу,
    разделу и версии проекта. FeedbackSample хранит только пару текстов
    без контекста, и восстановить по нему, к какому делу относится правка,
    невозможно.

    draft_text — снимок проекта на момент отзыва. Проект перегенерируют,
    и без снимка оценка «стала хуже» повисает без evidence.
    """

    __tablename__ = "feedback"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    # SET NULL, а не CASCADE: отзыв — самостоятельная ценность для обучения
    # и не должен исчезать вместе с делом.
    case_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("cases.id", ondelete="SET NULL"), nullable=True, index=True
    )
    draft_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("drafts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    draft_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Оценка 1..5 и итоговый вердикт. Оба поля обязательны: звёзды дают
    # «насколько», вердикт — «что с этим делать».
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    verdict: Mapped[str] = mapped_column(
        String(20), nullable=False, default=FeedbackVerdict.NEEDS_WORK.value, index=True
    )
    # Пусто = отзыв о проекте целиком; иначе — о конкретном разделе.
    section_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    comment: Mapped[str] = mapped_column(Text, default="", nullable=False)

    author_role: Mapped[str] = mapped_column(
        String(30), nullable=False, default=FeedbackAuthorRole.OTHER.value, index=True
    )
    author_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    # Какой моделью сформирован проект: без этого нельзя сравнить качество
    # версий модели между собой.
    model_name: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    draft_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_anonymized: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    def __repr__(self) -> str:
        return f"<Feedback {self.rating}/5 {self.verdict} role={self.author_role}>"
