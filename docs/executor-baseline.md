# Baseline перед DeepSeek-провайдером

- Дата: 2026-11-11
- Ветка: `work/QD-UI`
- Коммит: `165776a`
- Python: 3.14.3 (проект рассчитан на >=3.12)
- Тег: `baseline-2026-11-11` (локальный, не запушен)
- Незакоммиченное на старте: `docs/vm-remote-access.md` (чужая работа, не трогалась)

## pytest

Полный прогон:

```text
248 failed, 1081 passed, 6 warnings, 23 errors in 48.87s
```

Первые упавшие тесты:

- `backend/tests/test_agents.py::TestVerification::test_clean_draft_passes`
- `backend/tests/test_api_archived_cases.py::TestArchivedCases::test_hidden_case_missing_from_list`
- `backend/tests/test_api_cases.py::TestCreateCase::test_create_returns_201`
- `backend/tests/test_api_health.py::TestHealth::test_health_returns_200` (ERROR)
- `backend/tests/test_database.py::TestSchema::test_all_tables_created`

Что проверено отдельно:

- `test_api_health.py::TestHealth::test_health_returns_200` — **проходит** в одиночку.
- `test_database.py::TestSchema::test_all_tables_created` — **проходит** в одиночку.
- `test_api_cases.py` — **55 passed** в одиночку.
- `test_agents.py` — **1 failed** в одиночку (`test_clean_draft_passes`).
- `test_agents.py + test_api_archived_cases.py` — **1 failed** (тот же), остальные проходят.

Вывод: есть как минимум один реальный красный тест (`test_clean_draft_passes`) и зависимость полного прогона от порядка тестов. Это состояние baseline, а не следствие правок T0: в T0 менялся только этот файл и локальный тег.

## ruff

```text
All checks passed!
```

## mypy

```text
Found 39 errors in 7 files (checked 110 source files)
```

Основные файлы:

- `app/extraction/fact_extractor.py` — 14 ошибок;
- `app/api/routes/cases.py` — 11 ошибок;
- `app/validation/evidence.py` — 4 ошибки;
- `app/db/repositories.py` — 2 ошибки;
- `app/services/fact_service.py` — 3 ошибки;
- `app/datasets/court_decisions.py` — 1 ошибка;
- `app/ingestion/extract.py` — 1 ошибка.

## Решение T0

Baseline **красный**: pytest и mypy не проходят. По плану T0 («если baseline уже красный — не чинить, а остановиться и показать человеку») T1 не начинается до решения человека:

1. принять красный baseline как известное состояние и разрешить T1;
2. или сначала отдельной задачей починить `test_clean_draft_passes` и изоляцию тестов, затем повторить T0.

T0 не исправлял ни тесты, ни типы.
