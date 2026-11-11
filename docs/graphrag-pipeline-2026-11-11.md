# GraphRAG и multi-agent pipeline: что сделано

- Дата: 2026-11-11
- Ветка: `work/QD-UI`
- Коммиты: `9be5459`, последующие правки GraphRAG

## GraphRAG

`backend/app/graphrag.py` строит граф:

```
дело
 ├── документы
 ├── факты (claims / objections / evidence / legal_position)
 ├── суммы
 ├── нормы
 └── стороны
```

`context_for(section_key)` выбирает узлы не «первые восемь», а по
категориям, нужным разделу:

| Раздел | Приоритет категорий |
|---|---|
| established | claims, evidence, established_facts, protocol_findings |
| motivating | claims, objections, evidence, legal_position |
| operative | claims, decision_items, evidence |
| descriptive | claims, objections, evidence |

Включается:

```
GRAPHRAG_ENABLED=true
```

В `generate_draft` добавляет в промпт блок «СВЯЗАННЫЕ ФАКТЫ, СУММЫ И НОРМЫ».
По умолчанию выключено, чтобы не менять baseline.

## Multi-agent pipeline

`backend/app/agents/pipeline.py`:

```
research → verify_materials → synthesize → generate → review
```

Это явные стадии, а не автономные агенты. Pipeline:
- строит граф;
- включает graph-контекст для генерации;
- вызывает существующий `generate_draft`;
- проверяет результат `verify_draft`.

Включается:

```
MULTI_AGENT_ENABLED=true
```

`DraftService.generate` использует pipeline только при этом флаге.

## A/B на 15 реальных учебных делах

DeepSeek Flash, одинаковые дела:

| Вариант | Дел | P0 | REVIEW | Тяжёлых | Токены | ₽ | сек/дело |
|---|---:|---:|---:|---:|---:|---:|---:|
| baseline | 15 | 8 | 0 | 42 | 49 617 | 0,6346 | 52,2 |
| GraphRAG (первая версия) | 15 | 10 | 0 | 43 | 63 047 | 0,3798 | 53,5 |

Важно: это **не доказательство пользы GraphRAG**. В первой версии
`context_for` брал первые узлы, а не релевантные; после прогона отбор
переделан на категории. Нужен повторный A/B на новой версии.

Также в прогоне были сетевые ошибки ZvenoAI (часть разделов не
сгенерировалась), поэтому стоимость и P0 не сравнимы напрямую.

## Что проверено тестами

- граф строит факты, суммы, нормы, стороны;
- `context_for` возвращает связанный контекст;
- pipeline проходит стадии и возвращает draft/verification/graph;
- mock-прогон pipeline не падает.

## Ограничения

- GraphRAG in-memory, без Neo4j и без сохранения между запусками.
- Нет embeddings-ранжирования: отбор по категориям, а не по семантике.
- Pipeline не является автономным multi-agent: стадии фиксированы.
- A/B на 15 делах — пилот, не статистика.
- Флаги по умолчанию выключены; включать после повторного A/B.

## Следующий шаг

1. Повторить A/B baseline / GraphRAG / pipeline на 15–20 делах после
   изменения `context_for`.
2. Сравнить не только P0, но и:
   - пустые разделы;
   - подтверждённые суммы;
   - citation F1;
   - время и токены.
3. Включать флаги в установщике только после подтверждённого выигрыша.
