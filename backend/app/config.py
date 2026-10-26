"""Конфигурация CourtDraft AI.

Единственный модуль, который читает переменные окружения. Всё остальное
получает настройки черезDepends(get_settings) или через передачу параметров.

Ключевой инвариант: переключение MOCK -> REAL не требует правок в коде.
Различие только в LLM_PROVIDER / EMBEDDING_PROVIDER (п.61 ТЗ).
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent


class LLMProviderName(StrEnum):
    """Провайдеры генерации. Бизнес-логика знает только интерфейс."""

    MOCK = "mock"
    OLLAMA = "ollama"


class EmbeddingProviderName(StrEnum):
    """Провайдеры эмбеддингов (п.53)."""

    MOCK = "mock"
    LOCAL = "local"


class QdrantMode(StrEnum):
    """Режим Qdrant. На ноутбуке — memory, сервер нужен только на RTX 3050."""

    MEMORY = "memory"
    SERVER = "server"


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    RUNTIME = "runtime"


class Settings(BaseSettings):
    """Настройки приложения, загружаемые из .env."""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Приложение ──────────────────────────────────────────────────────
    app_name: str = "ИИ помощник судьи"
    app_env: AppEnv = AppEnv.DEVELOPMENT
    debug: bool = False
    log_level: str = "INFO"
    # S105: это не пароль, а заглушка; реальное значение приходит из .env,
    # которого в репозитории нет. Срабатывание ложное.
    secret_key: str = "change-me-in-production"  # noqa: S105

    # ── Главный переключатель MOCK / REAL ───────────────────────────────
    llm_provider: LLMProviderName = LLMProviderName.MOCK
    embedding_provider: EmbeddingProviderName = EmbeddingProviderName.MOCK

    # ── Ollama ──────────────────────────────────────────────────────────
    ollama_enabled: bool = False
    ollama_base_url: str = "http://host.docker.internal:11434"
    ollama_model: str = "qwen3.5:4b"
    ollama_timeout_sec: int = 120
    ollama_num_ctx: int = 4096
    ollama_temperature: float = 0.1

    # ── Embeddings (п.53) ──────────────────────────────────────────────
    embedding_dim: int = 1024
    embedding_model: str = "bge-m3"
    # Система обязана стартовать без модели эмбеддингов.
    embeddings_optional: bool = True

    # ── PostgreSQL ──────────────────────────────────────────────────────
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "courtdraft"
    postgres_user: str = "courtdraft"
    # S105: локальные credentials для разработки, переопределяются в .env.
    postgres_password: str = "courtdraft"  # noqa: S105
    # Пусто = выбирать автоматически (см. db.session.build_database_url).
    # Раньше здесь стоял URL из .env.example, и его нельзя было отличить от
    # «пользователь задал вручную»: приложение молча уходило в SQLite
    # вопреки явно прописанному PostgreSQL.
    database_url: str = ""

    # ── Qdrant ──────────────────────────────────────────────────────────
    qdrant_mode: QdrantMode = QdrantMode.MEMORY
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection_laws: str = "legal_laws"
    qdrant_collection_cases: str = "legal_cases"
    qdrant_collection_documents: str = "case_documents"

    # ── RAG (п.14, п.25) ───────────────────────────────────────────────
    chunk_size: int = 800
    chunk_overlap: int = 120
    retrieval_top_k: int = 10
    retrieval_candidates: int = 50
    rrf_k: int = 60

    # ── Ограничения ресурсов (п.54) ─────────────────────────────────────
    max_upload_mb: int = 50
    embedding_batch_size: int = 16
    concurrency_limit: int = 2
    request_timeout_sec: int = 300

    # ── Хранилище ───────────────────────────────────────────────────────
    # Относительные пути разрешаются от корня репозитория, а не от текущего
    # каталога: иначе запуск из backend/ создавал бы data/ внутри backend/
    # (проверено 07.10.2026). В .env.example тоже стоят абсолютные пути —
    # см. _resolve_path.
    storage_dir: Path = PROJECT_ROOT / "data" / "cases"
    documents_dir: Path = PROJECT_ROOT / "data" / "cases" / "documents"
    demo_dir: Path = PROJECT_ROOT / "data" / "demo"
    laws_dir: Path = BACKEND_DIR / "data" / "laws"
    templates_dir: Path = PROJECT_ROOT / "data" / "templates"
    # Каталог веб-интерфейса. Интерфейс раздаётся тем же приложением, что и
    # API: отдельный сервер и отдельный порт означали бы лишнюю настройку
    # и проблемы с происхождением запросов (CORS) без выигрыша.
    frontend_dir: Path = PROJECT_ROOT / "frontend"

    # ── CORS (п.44: только локально) ───────────────────────────────────
    # Хранится строкой, а не list[str]: pydantic-settings разбирает сложные
    # типы ДО вызова field_validator, поэтому список из .env ("a,b") падает
    # с SettingsError. Разбор вынесен в свойство cors_origin_list ниже.
    cors_origins: str = "http://localhost:3000"

    # ── Прочее ──────────────────────────────────────────────────────────
    pii_anonymize_demo: bool = True
    eval_cases: int = 20
    eval_top_k: int = 5

    @field_validator(
        "storage_dir", "documents_dir", "demo_dir", "laws_dir", "templates_dir",
        "frontend_dir",
        mode="after",
    )
    @classmethod
    def _resolve_paths(cls, value: Path) -> Path:
        """Разрешить относительный путь от корня репозитория.

        Без этого `STORAGE_DIR=./data/cases` из .env приводил к созданию
        data/ внутри backend/ — файлы оказывались не там, где их ищут,
        и пути в логах уезжали в корень диска.
        """
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @field_validator("chunk_overlap")
    @classmethod
    def _overlap_less_than_chunk(cls, value: int, info: object) -> int:
        """Перекрытие обязано быть меньше размера чанка, иначе chunking зациклится."""
        data = getattr(info, "data", {}) or {}
        chunk_size = data.get("chunk_size")
        if chunk_size is not None and value >= chunk_size:
            raise ValueError(
                f"CHUNK_OVERLAP ({value}) должен быть меньше CHUNK_SIZE ({chunk_size})"
            )
        return value

    # ── Производные свойства ────────────────────────────────────────────
    @property
    def is_real_mode(self) -> bool:
        """True, если включён настоящий ИИ, а не заглушка."""
        return self.llm_provider is not LLMProviderName.MOCK

    @property
    def cors_origin_list(self) -> list[str]:
        """Разобранный список Origins.

        Только локальные адреса (п.44). Пустое значение означает «не разрешать
        ничего» — это безопаснее дефолта с внешним хостом.
        """
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def ensure_dirs(self) -> None:
        """Создать каталоги данных. Вызывается при старте приложения.

        Каталог интерфейса не создаётся: он поставляется с репозиторием, и
        его отсутствие — это повреждённая поставка, а не состояние данных.
        """
        for path in (
            self.storage_dir,
            self.documents_dir,
            self.demo_dir,
            self.laws_dir,
            self.templates_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def frontend_available(self) -> bool:
        """Есть ли интерфейс.

        Приложение обязано работать и без него: бэкенд полезен сам по себе,
        а отсутствие файлов в frontend/ не должно выглядеть как поломка.
        """
        return (self.frontend_dir / "index.html").is_file()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Настройки приложения (с кэшем — читаются один раз)."""
    return Settings()
