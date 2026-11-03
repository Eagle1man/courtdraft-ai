# Диагностика стенда

Скрипты проверки живого стенда. Все создают только временные данные и
удаляют их за собой; чтение базы — в режиме `mode=ro`.

| Скрипт | Что проверяет | Что нужно |
|---|---|---|
| `check_ai_judge.py` | полный цикл: дело → материал → проект → проверка | `requests` |
| `check_isolation.py` | загрузка файла и изоляция по коду (свой 200, чужой 403) | `requests` |
| `check_e2e_ui.py` | браузер: два участника, вход по коду, смена кода, материалы | `requests`, `playwright` + Edge/Chrome |
| `check_case_card.py` | номер и суд подставляются из документов | `paramiko`, SSH-ключ |
| `collect_metrics.py` | выгрузка метрик теста в JSON | `paramiko`, SSH-ключ |

## Адреса и доступ

По умолчанию — тестовый стенд `10.19.20.20`. Переопределяется окружением:

```powershell
set COURTDRAFT_URL=http://127.0.0.1:8081        # для API и браузерных проверок
set COURTDRAFT_VM_HOST=10.19.20.20              # для SSH-проверок
set COURTDRAFT_VM_USER=ubuntuii
set COURTDRAFT_VM_KEY=C:\Users\...\.ssh\id_ed25519_lecture
```

## Запуск

```powershell
python scripts\diagnostics\check_ai_judge.py
python scripts\diagnostics\check_isolation.py
python scripts\diagnostics\check_e2e_ui.py
python scripts\diagnostics\collect_metrics.py metrics.json
```

Проверки на самой ВМ (через `localhost`) полезны, когда сеть до стенда
рвётся: `check_case_card.py` и `collect_metrics.py` ходят по SSH и работают
оттуда, поэтому от внешней сети не зависят.

## Чего эти скрипты не делают

Не публикуют, не перезапускают службу и не меняют схему базы. Выкладка —
отдельно, `scripts/vm_deploy_isolation.py`.
