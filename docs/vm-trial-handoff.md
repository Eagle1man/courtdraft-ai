# Передача смены: тестовый стенд CourtDraft AI

Адрес: **http://10.19.20.20:8081/app/**
ВМ: `ubuntuii@10.19.20.20`, проект `/home/ubuntuii/courtdraft-ai`.
Пароль в репозиторий не записан; для локальных скриптов задаётся
`COURTDRAFT_VM_PASSWORD`.

> **Важно (2026-11-12):** открывать интерфейс нужно со слэшем на
> конце. В текущей сборке на ВМ `/app` без слэша отдаёт HTML, но
> браузер запрашивает `app.js` и `styles.css` у корня и получает 404.
> Фикс (редирект `/app` → `/app/`) есть в коммите `7e207d9`, но на ВМ
> не развёрнут: SSH `10.19.20.20:22` недоступен.

## Состояние на момент передачи

- Backend: systemd-служба `courtdraft-backend`, порт **8081**, автозапуск.
- Ollama: systemd, 4 потока (`OLLAMA_NUM_THREAD=4`), одна модель в памяти.
- Этап теста: **первый** — все дела на `qwen2.5:1.5b`.
  - `MODEL_ROUTING_ENABLED=true`
  - `STRONG_MODEL=wavecut/vikhr:7b-instruct_0.4-Q4_1`
  - `WEAK_MODEL=qwen2.5:1.5b`
  - `STRONG_MODEL_QUOTA=0`
- `/api/health` — `OK`.
- База: SQLite, `data/cases/courtdraft.db`, режим WAL.
- UI: `app.js?v=0.6.0` (код участника + обратная связь).

## Что сделано в этой смене

### Квота моделей

- `backend/app/model_routing/quota.py` — первые N дел получают сильную
  модель, остальные — слабую. Назначение хранится в журнале аудита и
  фиксируется за делом.
- `POST /api/cases/{id}/draft` назначает модель до постановки задачи и
  передаёт её в фоновую генерацию.
- `scripts/vm_phase2.sh N` — включить второй этап (по умолчанию 5).

### Отзывы и разбор

- Модель отзыва берётся из `draft.generated`, а не из текущих настроек:
  сильная и слабая не смешиваются.
- `/api/feedback/stats` отдаёт `by_model` — count и средняя оценка.
- Код участника сохраняется в журнале `case.created` и связывает два
  дела одного студента.
- `backend/scripts/export_learning_data.py` — выгрузка в `data/learning/`
  (manifest, документы, проекты, отзывы). Для несинтетических дел нужен
  `--allow-personal`.

### Исправление «database is locked»

Причина: транзакция записи держалась всё время генерации (3–4 минуты).

Исправлено:

- `DraftService.generate` коммитит пакет доказательств до вызова модели;
- `ingestion.pipeline` коммитит до OCR и до эмбеддингов;
- SQLite: `journal_mode=WAL`, `busy_timeout=60000`, `BEGIN IMMEDIATE`
  для транзакций (`app/db/session.py`);
- проверка: 20 одновременных созданий дела — 20 успешных.

## Что осталось проверить

1. Журнал backend под реальной нагрузкой после последнего перезапуска:
   `sudo journalctl -u courtdraft-backend --since '-30 min' | grep -E 'database is locked|Traceback'`
2. Полный цикл при нескольких студентах: загрузка → генерация → отзыв.
3. После теста — выгрузка `export_learning_data.py` и разбор.

## Команды

```bash
# здоровье
curl -s http://127.0.0.1:8081/api/health

# второй этап: первым 5 делам Vikhr 7B
sudo bash /home/ubuntuii/courtdraft-ai/scripts/vm_phase2.sh 5

# отзывы по моделям
curl -s http://127.0.0.1:8081/api/feedback/stats

# выгрузка для разбора
cd /home/ubuntuii/courtdraft-ai/backend
PYTHONPATH=. .venv/bin/python scripts/export_learning_data.py \
  --out /home/ubuntuii/courtdraft-ai/data/learning --allow-personal
```

## Важно

- Перезапуск backend теряет незавершённые job'ы: их нужно запустить заново.
- Vikhr 7B на 4 vCPU идёт ~20–30 минут на проект. Второй этап можно
  запускать перед уходом студентов: очередь досчитается без них.
- Qdrant в памяти: после перезапуска поисковый индекс теряется, проекты
  в SQLite сохраняются.
