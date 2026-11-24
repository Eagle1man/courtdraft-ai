# AGENT_TASK_PASSPORT — задача для агента

## Цель

Добавить дополнительный способ подготовки существующего `00-ПАСПОРТ-ДЕЛА.json`
по материалам дела. Не переписывать генератор иска, схему паспорта и старые
валидаторы.

## Обязательный порядок

1. `python scripts/passport_audit.py`
2. Прочитать `artifacts/passport/ARCHITECTURE_AUDIT.md`.
3. Сверить `artifacts/passport/field_map.json` с реальным контрактом.
4. Создать/изменить только разрешённые файлы.
5. `python -m pytest backend/tests/test_passport_ai.py -q`
6. Регрессионный прогон старого сценария.
7. `python scripts/passport_eval.py`
8. Обновить `docs/passport_ai/IMPLEMENTATION_REPORT.md`.

## Жёсткие правила

- Никаких выдуманных полей, сумм, дат, адресов и реквизитов.
- Деньги — только `Decimal` внутри и `int` копеек в паспорте.
- Каждый LLM-факт проходит quote-gate и value-gate.
- Конфликты сохраняются все; выбор за человеком.
- Пустое поле лучше выдуманного.
- Сохранение — только после подтверждения человека и с бэкапом.
- Старый поток «готовый JSON → иск → DOCX» должен продолжать работать.
- Платные LLM-вызовы запрещены без явного разрешения.
- `student_code` обязателен на всех новых endpoints.
- Не менять `expected_claim.txt` и `tests/data/resol_fixtures.json`.

## Маршруты

- `POST /api/cases/{case_id}/passport-ai/analyze`
- `GET /api/cases/{case_id}/passport-ai/status`
- `GET /api/cases/{case_id}/passport-ai/analysis`
- `PATCH /api/cases/{case_id}/passport-ai/fields/{role}`
- `POST /api/cases/{case_id}/passport-ai/confirm`
- `POST /api/cases/{case_id}/passport-ai/documents/{document_id}/reprocess`

## Гейт

Новые тесты зелёные + старый сценарий не ухудшился + A25 зелёный.
Любая новая регрессия — стоп.
