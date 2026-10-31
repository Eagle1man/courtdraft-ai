# Дообучение Vikhr 7B на 4000 примерах (QLoRA + Ollama GGUF)

## 1. Подготовка датасета локально

В репозитории уже есть `prepare_dataset.py` в корне `D:\courtdraft-ai\`.

1. Положи все `.txt` и `.docx` файлы в `D:\courtdraft-ai\data\laws\raw\` (папка будет создана автоматически при первом запуске скрипта, если нужно).
2. Сделай пробный прогон (рекомендуется):

```bash
cd D:\courtdraft-ai
python prepare_dataset.py --limit 5
```

3. Если вывод корректный — полный прогон:

```bash
python prepare_dataset.py
```

Результат: `D:\courtdraft-ai\dataset.jsonl`. Формат: `{"query": "<имя файла без расширения>: <первые 100 символов>", "response": "<текст ≤1500 символов по границе слова>"}`. Один пример на файл.

## 2. Подготовка в ModelScope (облако)

### Регистрация и вход
1. Зарегистрируйся/зайди в [ModelScope](https://modelscope.cn/).
2. В терминале инстанса выполни: `modelscope login` (или авторизация через веб-интерфейс).

### Выбор инстанса
Для QLoRA 7B (4-bit) под эту задачу подойдут:
- **Рекомендуется:** A10 24 ГБ — хороший баланс цена/скорость
- **Допустимо:** A100 40/80 ГБ — быстрее
- **Минимум:** V100 32 ГБ
- **Впритык:** T4 16 ГБ (при малом batch_size может не хватить при больших длинах, 2 эпохи дольше)

Модель: `AI-ModelScope/Vikhr-7B-instruct_0.2`.

### Загрузка файлов
Скопируй на инстанс: `dataset.jsonl`, `finetune/train.sh`, `finetune/export.sh`, `finetune/Modelfile` (Modelfile нужен для финального шага локально, но можно загрузить сразу).

Варианты:
- Веб-загрузка в файловый менеджер инстанса
- `modelscope upload` из локальной машины
- `git`/`scp` — зависит от инстанса

Удобнее держать `dataset.jsonl` рядом с `finetune/` (либо поправь `DATASET` в `train.sh`).

## 3. Установка окружения

```bash
pip install --upgrade pip
pip install "ms-swift[all]" modelscope
```

Проверь, что `swift` доступен: `swift --version`.

## 4. Обучение (QLoRA)

Перейди в папку с обучающими скриптами:

```bash
cd finetune
bash train.sh
```

Скрипт использует флаги под ms-swift (с проверкой совместимости версий). Основные параметры: `lora_rank=8`, `lora_alpha=32`, `lr=1e-4`, `epochs=2`, `batch_size=2`, `grad_accum=8`, `max_length=2048`, `quantization_bit=4`.

Чекпоинты будут в `./output/`. На 4000 примеров обучение должно занять от ~1 до 3 часов в зависимости от GPU.

## 5. Слияние адаптера с базовой моделью

После окончания обучения найди папку с чекпоинтом (содержит `adapter_model.safetensors`/`adapter_config.json`).

Вариант 1 — автоматический поиск (последний найденный):
```bash
bash export.sh
```

Вариант 2 — явно указать путь:
```bash
export ADAPTERS=./output/AI-ModelScope/Vikhr-7B-instruct_0.2/vXXXX-XXXXXX/checkpoint-YYY
export OUT_MERGED=./vikhr-7b-qd-merged
bash export.sh
```

В результате получишь `./vikhr-7b-qd-merged/` в формате HuggingFace (safetensors + config + tokenizer).

## 6. Скачивание с ModelScope

Скачай папку `vikhr-7b-qd-merged/` на локальную машину (`D:\courtdraft-ai\finetune\` или отдельную папку). Можно через веб-интерфейс инстанса (download) или `modelscope download`.

## 7. Конвертация в GGUF (для Ollama)

Нам нужен GGUF Q4_K_M. Установи [llama.cpp](https://github.com/ggml-org/llama.cpp) локально (или в отдельной среде).

```bash
# Клонируй llama.cpp при отсутствии
git clone https://github.com/ggml-org/llama.cpp.git
cd llama.cpp
pip install -r requirements.txt
```

Конвертация из HF в GGUF (fp16):

```bash
python convert_hf_to_gguf.py /путь/к/vikhr-7b-qd-merged --outfile vikhr-7b-qd-f16.gguf
```

Квантизация в Q4_K_M:

```bash
./llama-quantize vikhr-7b-qd-f16.gguf vikhr-7b-qd.Q4_K_M.gguf Q4_K_M
```

Положи итоговый `vikhr-7b-qd.Q4_K_M.gguf` рядом с `finetune/Modelfile` (или поправь `FROM` в Modelfile).

> llama.cpp при конвертации обычно подтягивает `chat_template` из токенизатора — шаблон в `Modelfile` можно оставить как fallback, но лучше проверить после создания модели в Ollama.

## 8. Локальный запуск в Ollama

В той же папке, где лежат `Modelfile` и `vikhr-7b-qd.Q4_K_M.gguf`:

```bash
ollama create vikhr-qd -f Modelfile
ollama run vikhr-qd
```

Тестовый промпт:

```
[INST] Привет, кто ты? [/INST]
```

Если ответ корректный на русском — модель готова.

## Типовые ошибки
- **Out of memory** — уменьши `per_device_train_batch_size` до 1, увеличь `gradient_accumulation_steps` до 16.
- **`swift: command not found`** — `pip install "ms-swift[all]"`.
- **Неподходящий флаг** — смотри версию `swift --version`. Если 2.x, замени `--train_type lora` на `--sft_type lora` в `train.sh`.
- **Датасет не найден** — проверь путь в `DATASET`, либо скопируй `dataset.jsonl` в `finetune/`.
- **Битый gguf/не тот шаблон** — проверь вывод `ollama show vikhr-qd` (template), при необходимости поправь `TEMPLATE` в `Modelfile`.
