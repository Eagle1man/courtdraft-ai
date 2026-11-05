# Дообучение 7B на Kaggle

Готовый путь от нашего датасета до модели в Ollama. Рассчитан на
бесплатный Kaggle GPU (T4 16 ГБ, 30 часов в неделю).

## Что уже готово локально

| Файл | Размер | Что внутри |
|---|---|---|
| `D:\courtdraft-datasets\court-decisions-feb2026.jsonl` | 1,99 ГБ | 29 058 гражданских решений |
| `D:\courtdraft-datasets\sft-established-feb2026.jsonl` | 21 МБ | **1 413 пар** «материалы → установительная часть» |
| `D:\courtdraft-datasets\sft-established-mixed.jsonl` | 21,9 МБ | **1 434 пары**: корпус + 21 пара из решений, лежавших в материалах теста |

Пары из снимка собирает `scripts/dataset/extract_snapshot.py`: берёт
документы, в которых есть и «УСТАНОВИЛ», и «РЕШИЛ» (настоящие решения, а
не сгенерированные проекты), и строит пары тем же промптом, что и
сервис. Сгенерированные проекты в обучение не идут: в них есть
утверждения без источника, и учить на них — закреплять ошибки.

Датасет собран скриптом `scripts/dataset/build_sft.py` в том же формате
промптов, что использует сервис. Проверка формата:

```powershell
python scripts\dataset\build_sft.py --dry-run
```

## Запуск через CLI (без веб-интерфейса)

Всё то же можно сделать командами. Проверено 05.11.2026 на 1.5B:

```powershell
$env:KAGGLE_API_TOKEN = "<токен KGAT_...>"

# 1. датасет (файлы + обучающий скрипт рядом)
kaggle datasets create -p D:\courtdraft-datasets\kaggle-upload

# 2. ядро с GPU
kaggle kernels push -p D:\courtdraft-datasets\kaggle-kernel-v2

# 3. состояние и журнал
kaggle kernels status orel1986/courtdraft-1p5b-v2
kaggle kernels output orel1986/courtdraft-1p5b-v2 -p D:\courtdraft-datasets\kaggle-out-v2
```

Три грабли, на которых это падало:

1. **Путь к датасету не угадывать.** Kaggle монтирует датасет под слагом,
   и `/kaggle/input/<id>` может оказаться пустым каталогом. Скрипт
   запуска ищет файл через `rglob` по `/kaggle/input` и печатает дерево.
2. **`trl` не обновлять.** Unsloth 2026.11.2 требует `trl <= 1.15.0`;
   `pip install --upgrade trl` ставит 1.17 и обучение падает на импорте.
3. **BOM в `kernel-metadata.json`.** Файл, записанный PowerShell
   `Set-Content -Encoding UTF8`, начинается с BOM, и CLI падает
   с «Unexpected UTF-8 BOM». Писать без BOM (`UTF8Encoding($false)`).

Ещё: обновление уже созданного ядра (`SaveKernel`) иногда отваливается
по таймауту, а создание нового проходит. Если push завис — завести
ядро с новым id.

## Шаг 1. Загрузить датасет в Kaggle

1. Kaggle → Datasets → New Dataset.
2. Загрузить `sft-established-feb2026.jsonl` (21 МБ).
3. Назвать, например, `courtdraft-sft`. Запомнить путь вида
   `/kaggle/input/courtdraft-sft/sft-established-feb2026.jsonl`.

Проверочный файл отдельно не нужен: `val`-примеры помечены внутри
`meta.split`, скрипт сам отделит их по делу.

## Шаг 2. Ноутбук

GPU: `Settings → Accelerator → GPU T4 x1`. Интернет включён (нужен для
загрузки базовой модели).

```python
!pip install -q unsloth
!pip install -q --upgrade trl
```

```python
!python /kaggle/input/courtdraft-train/train_qlora.py \
    --train /kaggle/input/courtdraft-sft/sft-established-feb2026.jsonl \
    --output /kaggle/working/adapter \
    --model Qwen/Qwen2.5-7B-Instruct \
    --max-seq 4096 --epochs 2
```

Скрипт `train_qlora.py` лежит в репозитории (`scripts/kaggle/`). Его
можно приложить вторым Kaggle-датасетом или вставить в ячейку.

Ожидаемое время: 1–3 часа на T4 при 1 413 примерах и двух эпохах.
Лог печатает `train_loss` и `eval_loss` каждые 50 шагов.

## Шаг 3. Забрать адаптер

`/kaggle/working/adapter` → Download. Это LoRA-адаптер (десятки МБ),
не полная модель.

## Шаг 4. Слить и квантовать

На машине с GPU (или в той же Kaggle-сессии, если хватает диска):

```python
from unsloth import FastLanguageModel

model, tokenizer = FastLanguageModel.from_pretrained(
    model_name="/kaggle/working/adapter",
    max_seq_length=4096,
    load_in_4bit=False,
)
model.save_pretrained_merged("/kaggle/working/merged", tokenizer, save_method="merged_16bit")
model.save_pretrained_gguf("/kaggle/working/gguf", tokenizer, quantization_method="q4_k_m")
```

Диск Kaggle — 20 ГБ на рабочую папку: merged_16bit для 7B занимает
~15 ГБ, поэтому GGUF лучше собирать сразу и удалять merged. Если места
не хватит — квантовать на другой машине через `llama.cpp`:

```bash
python convert_hf_to_gguf.py merged --outfile model-f16.gguf
./llama-quantize model-f16.gguf model-q4_k_m.gguf Q4_K_M
```

## Шаг 5. Подключить в Ollama на стенде

```bash
ollama create courtdraft-7b -f Modelfile
```

`Modelfile`:

```
FROM ./model-q4_k_m.gguf
PARAMETER temperature 0.1
PARAMETER num_ctx 4096
PARAMETER stop "<|im_end|>"
```

Затем в `.env` стенда:

```
OLLAMA_MODEL=courtdraft-7b
```

Важно: `num_ctx` и `num_predict` в сервисе сейчас рассчитаны на 1.5B
(см. `max_tokens=420` в `draft_agent`). Перед A/B их надо поднять —
иначе 7B упрётся в лимит и оборвёт раздел.

## Шаг 6. Проверка

На стенде:

```powershell
python scripts\diagnostics\check_ai_judge.py
```

Смотреть те же метрики, что считали после теста: неверный кодекс,
выдуманные даты, пустая мотивировочная, доля утверждений без источника,
время на проект.

## Чего ожидать

| Что | Ожидание |
|---|---|
| Связность установительной части | заметно лучше |
| Пустые разделы | меньше |
| Соблюдение формата | лучше |
| Юридическая точность | почти без изменений — её даёт RAG по нормам |
| Скорость | 7B на T4-классе: ориентировочно 15–30 ток/с |

1 413 примеров — это мало для «выучить право», но достаточно, чтобы
выровнять стиль и структуру. Датасет надо расширять: наши собственные
выправленные проекты + пары из корпуса решений.

## Ограничения

- В датасете только решения **без маркеров обезличивания**: 20 064 из
  29 058 отброшены, иначе модель научилась бы писать `<ФИО>`.
- Ответы длиннее 8 000 знаков не включены: обрезка учила бы
  недоговаривать мотивы.
- Пары построены из решений, а не из материалов дел: вход — описательная
  часть решения. Для обучения на реальных материалах нужен экспорт с
  ВМ (`scripts/dataset/export_cases.py`, ещё не написан).
