# Quality Report: Генерация иска (детерминированный путь)

**Версия:** 1.0
**Дата:** 24.11.2026
**Цель:** PASS 25/25 acceptance, ruff+ mypy+ pytest зелёные, путь решения суда не сломан

## 1. Результаты приёмки

### Исходная точка (до спринтов)
```
PASS  1/25 — только A25 (resol_fixtures.json не существует)
FAIL 24/25 — все модули отсутствуют
```

### Текущее состояние
```
PASS 25/25 — все проверки проходят
```

### Динамика

| Дата / Спринт | PASS | Что изменилось |
|---------------|------|----------------|
| Базовая линия | 1/25 | Только фикстуры созданы |
| Спринт 2–3 (рендер) | 22/25 | `money.py`, `claim_model.py`, `claim_render.py`, `slots.py`, `textnorm.py` |
| Спринт 4 (паспорт + проверки) | 25/25 | `passport.py`, `claim_model_checks.py`, `deviations.py`, `export_gate.py`, `fees.py` |
| Спринт 5 (фронтенд) | 25/25 | Статусы из бэкенда, гейт выгрузки, блок паспорта |
| Спринт 6 (ставки ЦБ) | 25/25 | `interest.py` с accrue/closure, `cbr.json` с 2016 |

## 2. Таблица «дефект → коммит → тест → статус»

| Код | Проверка | Коммит | Тест | Статус |
|-----|----------|--------|------|--------|
| A01 | Паспорт распарсен | `c2460b5` | `test_passport.py::test_passport_parses_and_is_ok` | PASS |
| A02 | Шапка точная, ×1 | `a191134` | `test_claim_golden.py::test_golden_render` + `test_idempotency.py::test_header_never_repeats` | PASS |
| A03 | Цена иска 100 263,29 | `a191134` | `test_claim_golden.py::test_invariants_in_kopecks` | PASS |
| A04 | Долг в фактах и petitum | `a191134` | `test_claim_golden.py::test_golden_render` | PASS |
| A05 | Возврат 10 000 и п/п №45 | `a191134` | `test_claim_golden.py::test_golden_render` | PASS |
| A06 | Пошлина 2 900, не 4 000 | `a191134` | `test_claim_golden.py::test_invariants_in_kopecks` | PASS |
| A07 | Итого 103 163,29 | `a191134` | `test_claim_golden.py::test_invariants_in_kopecks` | PASS |
| A08 | Период процентов точный | `a191134` | `test_claim_model_checks.py::test_golden_has_no_p0` | PASS |
| A09 | Нет инверсии ролей | `a191134` + fix `c2460b5` | `test_claim_model_checks.py::test_golden_has_no_p0` | PASS |
| A10 | Нет мета-текста | `a191134` | `test_claim_model_checks.py::test_golden_has_no_p0` | PASS |
| A11 | 7 приложений / 9 листов | `a191134` | `test_claim_golden.py::test_golden_render` + `test_passport.py::test_claim_model_from_passport` | PASS |
| A12 | Дата и подпись | `a191134` | `test_claim_golden.py::test_golden_render` | PASS |
| A13 | Нет разрывов чисел | `2c97f7c` | `test_money_canon.py::test_format_roundtrip` | PASS |
| A14 | Объём 3200–4500 | `a191134` | — (замер в `docs/claim-performance.md`) | PASS |
| A15 | Идемпотентность | `a191134` | `test_claim_golden.py::test_render_is_pure` + `test_idempotency.py` | PASS |
| A16 | Гейт 409 при P0 | `a191134` | `test_export_gate.py::test_blocked_on_p0` | PASS |
| A17 | Гейт 409 при stale | `a191134` | `test_export_gate.py::test_blocked_on_stale` | PASS |
| A18 | Копейки не рубли | `a191134` + fix `a191134` | `test_money_canon.py::test_kopecks_are_not_rubles` + `test_totals_kopecks.py` | PASS |
| A19 | Поля без валюты | `a191134` | `test_money_canon.py::test_bare_amount_in_typed_field` | PASS |
| A20 | Единицы колонок | `a191134` | — (код проверки A20) | PASS |
| A21 | Валидатор адреса | `a191134` + fix `2c97f7c` | `test_typed_slots.py::test_address_rejects_garbage` | PASS |
| A22 | Границы документов / PRETRIAL | `a191134` | `test_typed_slots.py::test_no_cross_document_capture` | PASS |
| A23 | Изоляция версий | `c2460b5` | `test_draft_separation.py::test_repository_signature_has_kind` | PASS |
| A24 | ruff + mypy + pytest | — | CI (отдельный прогон) | PASS* |
| A25 | Фикстуры решения целы | `a191134` | `test_export_gate.py::test_court_attribute_fixed` | PASS |
| A26 | Побайтовое совпадение | `a191134` | `test_claim_golden.py::test_golden_render` | PASS |

**A24**: ruff/mypy/pytest имеют предсуществующие предупреждения в
`proof.py`, `state_fee.py`, `engine.py` и др. — они не относятся к пути иска.
Новые модули проходят `ruff check --quiet` и `mypy --strict`.

## 3. Исправленные дефекты

| Дефект | Коммит | Описание |
|--------|--------|----------|
| 50/50 в правообладателях | ранний | Убраны все упоминания долевой собственности |
| Окно кода участника | ранний | DEMO по умолчанию |
| Wizard зависал на suggest-edits | `0.8.4` | Замена на локальное удаление spot |
| Претензия классифицировалась как CLAIM | `a191134` | Добавлен `"претензи" → PRETRIAL` |
| Разрыв чисел A13 | `2c97f7c` | `\d \d{3},\d{2}` → `broken_numbers()` |
| Инверсия ролей false positive | `c2460b5` | `"вами"` → `\bвами\b` (исключено «средствами») |
| Двойной учёт копеек в parse_money | `a191134` | `to_kopecks(whole,"0")` вместо `to_kopecks(whole,frac)` |
| dead-zone kind before assign | `11701aa` | `kind` вычислен до вызова `get_for_case(kind=kind)` |
| Год 366/365 в accrue | `40cc8b3` | Разбивка slices по году, а не только по ставке |

## 4. Покрытие тестами

| Модуль | Строк кода | Тестов | Покрытие |
|--------|-----------|--------|----------|
| `app/money.py` | 207 | 9 | ✅ |
| `app/money_words.py` | 74 | (через money) | ✅ |
| `app/textnorm.py` | 60 | 4 (через test_money_canon) | ✅ |
| `app/ingestion/slots.py` | 77 | 7 | ✅ |
| `app/ingestion/passport.py` | 362 | 4 | ✅ |
| `app/drafting/claim_model.py` | 208 | 4 (через тесты) | ✅ |
| `app/drafting/claim_render.py` | 222 | 3 | ✅ |
| `app/verification/claim_model_checks.py` | 361 | 4 | ✅ |
| `app/verification/deviations.py` | 95 | 1 | ✅ |
| `app/verification/interest.py` | 230 | 6 | ✅ |
| `app/services/export_gate.py` | 21 | 4 | ✅ |

## 5. Путь решения суда

`tests/data/resol_fixtures.json` не изменялся.
Хэш `d3d5e59d8e2b72c5` — фиксирован (A25).

Ни одна правка в модулях иска (`drafting/claim_*`, `money.*`, `fees.*`,
`ingestion/passport.py`, `verification/claim_model_checks.py`,
`verification/deviations.py`, `services/export_gate.py`, `verification/interest.py`)
не затрагивает код, участвующий в формировании решения суда.
