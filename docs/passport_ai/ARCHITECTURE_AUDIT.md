# ARCHITECTURE_AUDIT — паспорт-ИИ

Дата: 2026-11-24
Источник: `artifacts/passport/ARCHITECTURE_AUDIT.md` + реальный код проекта.

## 1. Существующая архитектура

- Backend: Python 3.14, FastAPI, SQLAlchemy async, SQLite.
- Документы: `CaseDocument.text` + `DocumentChunk` с provenance.
- Паспорт: `app.ingestion.passport.CasePassport` (Pydantic v2).
- Генератор иска: `app.drafting.claim_draft.generate_claim_draft` →
  `ClaimModel.from_passport` → `render_claim`.
- Проверка: `CasePassport.validate_invariants()`.
- Экспорт: `app.services.export_docx` + `export_gate`.
- Frontend: vanilla JS, существующие `state`, `api()`, `withStudentCode()`.

## 2. Реальный контракт паспорта

Образец: `backend/tests/fixtures/case_2-101-2025/00-ПАСПОРТ-ДЕЛА.json`.

Верхний уровень:
`schema`, `mode`, `case`, `parties`, `money_kopecks`, `interest`, `claims`,
`contract`, `events`, `suit_date`, `fee_receipt`, `pretrial`, `suit_rpo`,
`law`, `documents`, `gaps`.

Критичные особенности:
- деньги — целые копейки (`int`);
- даты — `ДД.ММ.ГГГГ`;
- `claims`, `events`, `documents` — массивы;
- обязательные блоки: `case.court_for_header`, `parties.plaintiff.full`,
  `parties.defendant.full`, `money_kopecks.*`, `interest.from/to/days/base_kopecks`,
  `contract.date/due_date`, `fee_receipt.id/date/amount_kopecks`, `suit_date`.

## 3. Существующая проверка

`CasePassport.model_validate()` проверяет структуру.
`validate_invariants()` проверяет арифметику (И-1…И-14), даты, обязательные слоты,
пробелы и приложения. Ошибки P0 блокируют подтверждение/экспорт.

## 4. Существующая генерация иска

`generate_claim_draft()` находит паспорт в материалах или БД, строит `ClaimModel`,
вызывает чистый `render_claim()`. LLM в этом пути не участвует.

## 5. Точки интеграции

- Новый роутер добавлен в существующий `app/api.routes.passport.router`.
- Пути изолированы префиксом `/passport-ai`, старые `/passport` не изменены.
- Схемы расширены классами `PassportAIStatusOut`, `PassportFieldEditIn`,
  `PassportAIAnalysisOut`.
- `main.py` не менялся: роутер уже подключён.
- Frontend-блок добавлен после `#facts`, скрипт `passport.js` подключён после `app.js`.

## 6. Обработка документов

Новый модуль **не читает файлы повторно**. Адаптер `reader.from_db_documents()`
преобразует `CaseDocument` и `DocumentChunk` во фрагменты с локаторами.
OCR, pypdf и python-docx в новом контуре не запускаются.

## 7. LLM

Единый интерфейс `LLMClient`. По умолчанию `FakeLLM`. Реальный провайдер
включается только явно (`PASSPORT_LLM_MODE=host|openai|auto`). Платные вызовы
в тестах и оценке запрещены.

## 8. Ограничения

- Вложенный контракт не поддерживает «плоский» field_map из поставки.
- `claims/events/documents` собираются правилами `derived` из `field_map.json`.
- Полный регрессионный набор проекта содержит три предсуществующих красных
  теста, не связанных с паспорт-модулём (см. IMPLEMENTATION_REPORT).

## 9. Новая архитектура

Документы БД → reader → classify → extract (LLM + regex, quote/value-gate) →
reconcile → assembler → существующий валидатор → ручное подтверждение →
`CasePassportRepository` с бэкапом.

## 10. Файлы

Созданы `backend/app/passport_ai/*`, `scripts/passport_audit.py`,
`scripts/passport_eval.py`, `backend/tests/test_passport_ai.py`,
`frontend/passport.js`, `artifacts/passport/*`, `data/passport_eval/*`,
`docs/passport_ai/*`, `AGENT_TASK_PASSPORT.md`.

Изменены существующие файлы:
- `backend/app/api/routes/passport.py` — только новые `/passport-ai/*` маршруты;
- `backend/app/schemas/passport.py` — только новые AI-схемы;
- `frontend/index.html` — блок и подключение скрипта;
- `frontend/styles.css` — стили блока.
