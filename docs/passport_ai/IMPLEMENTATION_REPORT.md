# IMPLEMENTATION_REPORT — паспорт-ИИ

Дата: 2026-11-24
Ветка: `work/QD-UI`

## 1. Что найдено аудитом

- Реальный паспорт найден в `backend/tests/fixtures/case_2-101-2025/00-ПАСПОРТ-ДЕЛА.json`.
- Контракт вложенный; деньги — `int` копеек; даты — `ДД.ММ.ГГГГ`.
- Существующий валидатор: `CasePassport.model_validate()` + `validate_invariants()`.
- Существующий генератор иска использует `ClaimModel` + `render_claim()`.
- Документы уже находятся в БД (`CaseDocument`, `DocumentChunk`).
- Старый роутер `/api/cases/{id}/passport` уже подключён в `main.py`.

Подробности: `docs/passport_ai/ARCHITECTURE_AUDIT.md`.

## 2. Итоговый field_map

Файл: `artifacts/passport/field_map.json`.

Основные роли:
- `plaintiff_name` → `/parties/plaintiff/full`;
- `defendant_name` → `/parties/defendant/full`;
- `court_name` → `/case/court_for_header`;
- `obligation_amount` → `/money_kopecks/loan`;
- `paid_amount` → `/money_kopecks/repaid`;
- `debt_amount` → `/money_kopecks/principal`;
- `interest_amount` → `/money_kopecks/interest`;
- `fee_paid` → `/money_kopecks/fee_paid` + `/fee_receipt/amount_kopecks`;
- `claim_amount` → `/money_kopecks/price`;
- `claim_total` → `/money_kopecks/total`;
- `contract_date` → `/contract/date`;
- `contract_due_date` → `/contract/due_date`;
- `interest_from/to/days/base` → `/interest/*`;
- `fee_receipt_id/date` → `/fee_receipt/*`;
- `suit_date` → `/suit_date`.

Массивы `claims`, `events`, `documents` собираются по `derived`-правилам.
Внешний валидатор: `app.passport_ai.validators:validate_existing`.

## 3. Созданные модули

- `backend/app/passport_ai/__init__.py`
- `models.py`
- `llmclient.py`
- `reader.py`
- `classify.py`
- `extract.py`
- `reconcile.py`
- `assembler.py`
- `validators.py`
- `service.py`
- `backend/app/api/routes/passport.py` — добавлены 6 `/passport-ai/*` маршрутов
- `backend/app/schemas/passport.py` — добавлены AI-схемы
- `frontend/passport.js` + блок в `index.html` + стили
- `scripts/passport_audit.py`
- `scripts/passport_eval.py`
- `backend/tests/test_passport_ai.py`
- `data/passport_eval/case-01..10`
- `docs/passport_ai/*`
- `AGENT_TASK_PASSPORT.md`

## 4. Результаты тестов

### Новые тесты
```text
python -m pytest tests/test_passport_ai.py -q
22 passed
```

### Регрессионный набор
Зелёных тестов в выбранном наборе: **66**.
Три предсуществующих красных теста не связаны с новым модулем:
- `test_totals_kopecks.py::test_total_is_not_29`;
- `test_totals_kopecks.py::test_total_priority_total_over_itogo`;
- `test_typed_slots.py::test_no_500_char_slice`.

Приёмка иска: **PASS 25/25**, A25 зелёный.

### Ruff / mypy
- `ruff` по новым backend-файлам, роутеру, схемам и тестам: **All checks passed**.
- `mypy` по новым файлам: ошибок в `passport_ai`, новом роутере и новых схемах нет.
- Полный `mypy app` остаётся красным из-за предсуществующих ошибок в
  `case_evidence`, `verification`, `ingestion` и других старых модулях.

## 5. Baseline оценки

Фактические значения: `docs/passport_ai/EVAL_BASELINE.json`.

| Метрика | Мин | Макс | Среднее |
|---|---:|---:|---:|
| Field Extraction Precision | 0.0 | 1.0 | 0.848 |
| Field Extraction Recall | 0.0 | 1.0 | 0.848 |
| Field-level F1 | 0.0 | 1.0 | 0.848 |
| Evidence Coverage | 0.0 | 1.0 | 0.900 |
| Conflict Detection Recall | 1.0 | 1.0 | 1.000 |
| Processing Time, сек | 0.042 | 0.120 | 0.065 |

Нулевые precision/recall относятся к намеренно пустому/повреждённому кейсу,
где правильный результат — оставить все поля пустыми (`correct_empty=20/20`).

## 6. Безопасность и совместимость

- Quote-gate и value-gate включены.
- Неподтверждённые факты сохраняются как `UNSUPPORTED`, но не попадают в паспорт.
- По умолчанию FakeLLM; платные вызовы только при явном `PASSPORT_LLM_MODE`.
- `student_code` проверяется на всех новых endpoints.
- `confirm()` не пишет файл напрямую; сохранение идёт через существующий
  `CasePassportRepository` с бэкапом.
- Старый сценарий готового JSON не изменён.

## 7. Живая проверка установленной копии

На деле `4150a7a6-d831-44b6-8f1b-3eaa250cf5ef` (11 документов, включая
паспорт и реальные DOCX ПАПКА2) выполнен `/passport-ai/analyze`:

- `stage=validated`;
- `fields_filled=31`;
- `conflicts=0`;
- `blocking_issues=0`;
- `can_confirm=True`;
- реальный DeepSeek включён через `PASSPORT_LLM_MODE=host`;
- синхронный хост-провайдер обёрнут в `asyncio.to_thread`, поэтому статус
  отвечает во время анализа;
- существующий `00-ПАСПОРТ-ДЕЛА.json` исключён из исходников и не загрязняет факты;
- суммы: займ 10 000 000, возврат 1 000 000, долг 9 000 000,
  проценты 1 026 329, пошлина 290 000, итог 10 316 329 копеек;
- конфликтов нет;

## 8. Что осталось

1. Реальный LLM-режим проверен на одном деле; для других дел нужен контроль
   качества и стоимости вызовов.
2. В полном проекте остаются предсуществующие красные тесты `totals` и
   `claim_suggest` — они не относятся к паспорт-модулю.
3. В UI ручное редактирование выполнено через поле ввода + кнопку подтверждения;
   отдельный многовариантный выбор конфликта пока не реализован.
4. Для production требуется синхронизировать новые файлы в установленную копию
   `C:\Pomoshnik-v-sude` и перезапустить сервер.
