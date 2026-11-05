# Текущая архитектура CourtDraft AI

Составлено по коду, а не по отчётам. Дата: 06.11.2026, ветка `work/QD-UI`.
Всё, что помечено «проверено», подтверждено чтением файла или запуском.

## 1. Что это

FastAPI-приложение для подготовки проектов судебных актов по материалам
дела. Работает полностью локально: LLM и эмбеддинги — через Ollama,
векторное хранилище — Qdrant в файловом режиме, данные — SQLite.

Стек: Python 3.12 (стенд) / 3.14 (разработка), FastAPI, async SQLAlchemy,
Alembic, Pydantic v2, Ollama, Qdrant, Tesseract.

## 2. Поток данных (проверено)

```
загрузка файла
  → app/ingestion/extract.py        извлечение текста по форматам
  → app/ingestion/ocr.py            Tesseract, если нет текстового слоя
  → app/ingestion/chunking.py       чанки по структуре
  → app/ingestion/jurisdiction.py   вид судопроизводства по тексту
  → app/agents/evidence_pack.py     пакет доказательств (категории)
  → app/rag/laws.py                 нормы ГК/ГПК/АПК в подсказку
  → app/agents/draft_agent.py       генерация по разделам шаблона
  → app/verification/engine.py      проверка проекта (50 типов замечаний)
  → app/services/draft_service.py   версия + отчёт проверки в БД
```

Разделы шаблона `civil_judgment`: `introductory`, `descriptive`,
`objections`, `established`, `motivating`, `operative`. Для арбитражных
дел — `arbitration_judgment` (нормы АПК).

## 3. Модули

| Путь | Строк | Ответственность |
|---|---|---|
| `app/api/routes/cases.py` | 551 | дела, документы, участники, загрузка |
| `app/api/routes/drafts.py` | 613 | генерация, задания, версии, проверка, claims |
| `app/api/routes/feedback.py` | — | отзывы участников и выгрузка |
| `app/api/routes/system.py` | — | health, info, ready |
| `app/services/draft_service.py` | 380 | сборка пакета, генерация, сохранение версии |
| `app/agents/draft_agent.py` | 1207 | промпты, генерация разделов, защита от выдумок |
| `app/agents/evidence_pack.py` | 636 | пакет доказательств, категории, цитаты |
| `app/verification/engine.py` | 1188 | движок проверки, 50 типов замечаний |
| `app/verification/resol_checks.py` | 530 | резолютивная часть (25 проверок) |
| `app/drafting/requirements.py` | 1315 | разбор требований |
| `app/drafting/operative.py` | 824 | резолютивная часть |
| `app/drafting/numeric_guard.py` | 190 | суммы: сверка с материалами |
| `app/drafting/date_guard.py` | — | даты: сверка с материалами |
| `app/ingestion/extract.py` | 444 | DOCX, PDF, HTML, JSON, TXT, MD |
| `app/ingestion/ocr.py` | — | Tesseract для сканов |
| `app/ingestion/chunking.py` | 321 | чанки с привязкой к странице и абзацу |
| `app/rag/laws.py`, `app/rag/store.py` | 149 / 310 | нормы: индекс и поиск |
| `app/services/job_recovery.py` | — | возобновление заданий после перезапуска |
| `app/db/repositories.py` | 593 | доступ к данным |
| `app/models/entities.py` | 535 | 16 таблиц |

## 4. Данные

Таблицы: `cases`, `case_documents`, `document_chunks`, `case_analyses`,
`participants`, `drafts`, `draft_versions`, `verifications`,
`evidence_items`, `jobs`, `audit_entries`, `feedback`, `feedback_samples`,
`law_documents`, `judgment_templates`, `court_decisions`.

Провенанс фрагментов: `document_chunks` хранит `page`, `paragraph`,
`character_start`, `character_end` — этого достаточно, чтобы ссылаться на
место в документе.

Версии проекта: `draft_versions` (полный текст каждой версии, автор
`ai`/`user`, `citations` — разделы с цитатами). Отчёт проверки привязан к
версии (`verifications.draft_version`), устаревший отчёт помечается как
`is_stale`.

## 5. API (28 эндпоинтов, проверено)

| Группа | Эндпоинты |
|---|---|
| Дела | `POST/GET /api/cases`, `GET/PATCH/DELETE /api/cases/{id}` |
| Документы | `POST /api/cases/{id}/documents/upload`, `POST /api/cases/{id}/documents`, `GET …/documents` |
| Участники | `GET/POST /api/cases/{id}/participants` |
| Генерация | `POST /api/cases/{id}/draft` (202 + job_id, либо `wait=true`) |
| Задания | `GET /api/jobs/{id}` |
| Проект | `GET /api/cases/{id}/draft`, `GET …/draft/versions`, `POST /api/drafts/{id}/versions` |
| Проверка | `POST /api/drafts/{id}/reverify`, `GET /api/drafts/{id}/claims` |
| Пакет | `GET /api/cases/{id}/evidence` |
| Статус | `PATCH /api/drafts/{id}/status` |
| Отзывы | `POST/GET /api/feedback`, `GET /api/feedback/stats`, `…/export` |
| Служебные | `GET /api/health`, `/api/info`, `/api/ready`, `/api/demo/manifest` |

## 6. Проверки (50 типов, проверено)

`app/verification/engine.py:57` — `IssueType`. Группы:

- **источники**: `UNSUPPORTED_CLAIM`, `MISSING_SOURCE`, `INCORRECT_QUOTE`,
  `CITATION_DANGLING`;
- **право**: `LAW_NOT_FOUND`, `PRACTICE_NOT_FOUND`, `FOREIGN_CODE`,
  `FORBIDDEN_REF`, `PENALTY_BASIS_UNCONFIRMED`;
- **согласованность**: `CONTRADICTION`, `INCOMPLETE`,
  `JURISDICTION_MISMATCH`, `ARBITRATION_JURISDICTION`;
- **процедура**: `PROCEDURE_ORDER`, `PROCEDURE_SIMPLIFIED`, `PRETRIAL_UNADDRESSED`,
  `STATEMENT_LIMITATION`, `STATEMENT_PENALTY_REDUCTION`;
- **доказательства**: `EVIDENCE_NOT_ASSESSED`, `PLEADING_AS_EVIDENCE`,
  `EXPERT_UNASSESSED`, `CLAIM_AMOUNT_UNPARSED`;
- **резолютивная часть**: 25 проверок `RESOL_*` (расходы, суммы, стороны,
  обжалование, сроки, доли, заочное производство и др.).

Важность: `LOW / MEDIUM / HIGH / CRITICAL`.

## 7. Фоновые задания

`jobs` в БД — источник истины, реестр в памяти — быстрый кэш. Генерация
идёт в `BackgroundTasks` с семафором на один процесс. После перезапуска
служба забирает `PENDING`/`RUNNING` и **повторяет** их (не более
`JOB_RESUME_MAX_ATTEMPTS`), кроме случая, когда версия уже сохранена —
тогда задание помечается готовым без дублирования.

## 8. База знаний по нормам

Индекс: 4 358 пассажей (ГК РФ, ГПК РФ, АПК РФ), эмбеддинги `bge-m3`,
Qdrant в файловом режиме (`QDRANT_MODE=local`). Поиск подмешивается
только в разделы `established` и `motivating`.
Замер на стенде: recall@1 0,517, recall@5 0,787, MRR 0,619 (4 205 запросов).

## 9. Тесты и качество кода

- `pytest`: **1 258 тестов**, все проходят.
- `ruff check app tests`: чисто.
- Миграции: `alembic upgrade head` с нуля проверяется.

## 10. Ограничения, подтверждённые кодом

1. **Таблицы DOCX не извлекаются.** `extract.py:348` читает только
   `document.paragraphs`; таблицы лишь считаются в метаданных
   (`"tables": len(document.tables)`). Содержимое таблиц — расчёты,
   реквизиты, графики платежей — в пакет доказательств не попадает.
   Это прямо объясняет часть `UNSUPPORTED_CLAIM` по суммам.
2. **Факты не версионируются.** Пакет пересобирается на каждую
   генерацию; реестра фактов с историей и ручными правками нет.
3. **Цитаты не проверяются на присутствие в источнике.** Проверка сумм и
   дат идёт по вхождению значения в материалы, а не по цитате.
4. **Противоречия текстовые.** `CONTRADICTION` — эвристика по тексту
   проекта, реестра расхождений между документами нет.
5. **Нет автомата состояний дела.** Есть статус дела и статус задания,
   но не постадийный процесс с чекпоинтами.
6. **Нет API для фактов и утверждения человеком.** Правка возможна только
   через новую версию текста.
7. **Docling не используется.** Сравнения с лёгкими парсерами не было.
