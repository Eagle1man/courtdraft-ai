# TESTING — паспорт-ИИ

## Запуск

```bash
cd backend
python -m pytest tests/test_passport_ai.py -q
```

Тесты не используют сеть и реальную LLM: только `FakeLLM` и синтетические тексты.

## Покрытие

- reader: текст и чанки БД, локаторы, дубликаты;
- money: `Decimal`, отсутствие денег;
- quote-gate: отказ на пересказе;
- value-gate: отказ на выдуманном значении с реальной цитатой;
- инъекции: факт не создаётся;
- конфликты: два разных значения не разрешаются молча;
- арифметика: несовпадение долга фиксируется;
- assembler: вложенные указатели, копейки, даты;
- обязательные поля: пустое поле блокирует подтверждение;
- end-to-end: FakeLLM + детерминированный добор;
- реальный эталон 2-101/2025: `can_confirm=True`, 0 конфликтов, 0 P0;
- старый поток: готовый паспорт → рендер иска продолжает работать.

## Оценочный набор

```bash
python scripts/passport_eval.py
```

10 синтетических сценариев в `data/passport_eval/`:
complete, missing document, different amounts, different dates, partial payment,
incomplete requisites, ambiguous, duplicates, corrupted, conflicting statements.

Baseline зафиксирован в `EVAL_BASELINE.json`. Числовые пороги не выдумывались.

## Предсуществующие красные тесты проекта

Полный регрессионный набор до паспорт-модуля уже содержал:
- `test_totals_kopecks.py::test_total_is_not_29`;
- `test_totals_kopecks.py::test_total_priority_total_over_itogo`;
- `test_typed_slots.py::test_no_500_char_slice`.

Они не связаны с новым модулем и не удалялись/не ослаблялись.
