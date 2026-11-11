# Baseline перед DeepSeek-провайдером

- Дата: 2026-11-11
- Ветка: `work/QD-UI`
- Коммит до починки: `165776a`
- Python: 3.14.3 (проект рассчитан на >=3.12)
- Тег: `baseline-2026-11-11` (локальный, не запушен)
- Незакоммиченное на старте: `docs/vm-remote-access.md` (чужая работа, не трогалась)

## Первый замер и его причина

Первый полный прогон выполнялся из корня репозитория:

```text
cd D:\courtdraft-ai
backend\.venv\Scripts\python.exe -m pytest -q
```

Результат: `248 failed, 1081 passed, 23 errors`.

Этот результат **не был состоянием кода**. Настройки pytest лежат в
`backend/pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

При запуске из корня без аргументов pytest не находит этот файл, поэтому
`asyncio_mode = auto` не применяется. Асинхронные тесты падают с
`async def functions are not natively supported`, а незакрытые
соединения и циклы событий дают каскад из 23 errors и сотен
последующих падений. Это выглядело как зависимость от порядка тестов.

Правильный запуск — из каталога `backend`:

```powershell
cd D:\courtdraft-ai\backend
.\.venv\Scripts\python.exe -m pytest
```

## Реальный дефект, найденный при разборе

`test_agents.py::TestVerification::test_clean_draft_passes` падал и при
правильном запуске. Причина — в `verify_draft`:

```python
registry = LawRegistry.load(settings.laws_dir if settings else Path("data/laws"))
```

Путь `data/laws` был относительным: он зависел от текущего каталога.
В корне репозитория лежит пустой `data/laws` (только `.gitkeep`), а
действующая база законодательства — в `backend/data/laws`
(`gpk.json`, `gkk.json`, `nk.json`). Поэтому ст. 56 ГПК считалась
отсутствующей, и чистый проект получал `LAW_NOT_FOUND`.

Исправление: база всегда загружается из настроек, а не из текущего каталога.

```python
registry = LawRegistry.load((settings or get_settings()).laws_dir)
```

Правила проверки не ослаблялись, тесты не переписывались.

## mypy

Исходно: `Found 39 errors in 7 files (checked 110 source files)`.

Что исправлено:

- `extraction/fact_extractor.py` — разные значения в циклах получили разные
  имена (`amount_value`, `date_value`, `claim_amount`, `fact_value`,
  `participant_key`, `amount_key`, `date_key`); протоколы `ChunkLike` и
  `JsonProvider` типизированы, `CATEGORY_PROMPTS` — `type[BaseModel]`.
- `extraction/schemas.py` — роль участника вынесена в `ParticipantRole`,
  `normalize_role` возвращает этот тип.
- `api/routes/cases.py` — результат `_ensure_case_visible` присваивается
  обратно в `case`; функция уже возвращает `Case` или бросает 404.
- `validation/evidence.py` — общий список источников типизирован объединением
  моделей; словари сумм и дат получили конкретные типы элементов.
- `db/repositories.py` — результат `UPDATE` приведён к `CursorResult[Any]`
  перед чтением `rowcount`.
- `services/fact_service.py` — сессии аннотированы `AsyncSession`.
- `ingestion/extract.py` — `Paragraph` и `Table` импортируются напрямую.
- `datasets/court_decisions.py` — у `_find_heading` есть возвращаемый тип.

## Итоговые проверки

```text
cd D:\courtdraft-ai\backend
.\.venv\Scripts\python.exe -m pytest
1352 passed in 80.58s (0:01:20)

.\.venv\Scripts\python.exe -m ruff check app tests
All checks passed!

.\.venv\Scripts\python.exe -m mypy app
Success: no issues found in 110 source files
```

Baseline зелёный. T1 можно начинать.
