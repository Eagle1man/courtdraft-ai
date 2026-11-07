"""Упаковка LoRA-адаптера 7B в GGUF для Ollama.

Почему не слияние с базой, как в 1.5B-версии: слияние 7B требует ~15,5 ГБ
оперативной памяти и столько же на диске, а затем ещё ~15 ГБ на f16-GGUF —
это упирается в 20 ГБ рабочего каталога Kaggle. LoRA-GGUF занимает 160 МБ,
а Ollama применяет его к уже скачанной базе через `ADAPTER` в Modelfile.

Итог: courtdraft-7b-lora.gguf + база qwen2.5:7b на стенде.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import traceback

WORK = pathlib.Path("/kaggle/working")
# llama.cpp — инструмент, а не результат: держим его вне /kaggle/working,
# иначе тысячи файлов репозитория попадают в вывод ядра и в скачивание.
_SCRATCH = pathlib.Path("/kaggle/temp") if pathlib.Path("/kaggle/temp").is_dir() else WORK
LLAMA = _SCRATCH / "llama.cpp"
BASE_MODEL_ID = "Qwen/Qwen2.5-7B-Instruct"
OUTFILE = WORK / "courtdraft-7b-lora.gguf"


def run(command: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print("+", " ".join(str(part) for part in command), flush=True)
    return subprocess.run(command, check=check)


def find_adapter() -> pathlib.Path:
    print("содержимое /kaggle/input:", flush=True)
    for root in sorted(pathlib.Path("/kaggle/input").iterdir()):
        print(" ", root, flush=True)
        if root.is_dir():
            for child in sorted(root.iterdir())[:8]:
                print("    ", child.name, flush=True)
    candidates = []
    for config in pathlib.Path("/kaggle/input").rglob("adapter_config.json"):
        folder = config.parent
        if (folder / "adapter_model.safetensors").exists():
            candidates.append(folder)
    if not candidates:
        raise SystemExit("адаптер не найден: нет adapter_config.json рядом с adapter_model.safetensors")
    # Предпочитаем итоговый адаптер (вне checkpoint-*), а не промежуточный чекпоинт.
    candidates.sort(key=lambda path: ("checkpoint-" in str(path), len(str(path))))
    print("адаптер выбран:", candidates[0], flush=True)
    print((candidates[0] / "adapter_config.json").read_text(encoding="utf-8"), flush=True)
    return candidates[0]


def main() -> None:
    adapter = find_adapter()

    run([sys.executable, "-m", "pip", "install", "-q", "gguf", "sentencepiece", "safetensors"])
    run(["git", "clone", "--depth", "1", "https://github.com/ggml-org/llama.cpp", str(LLAMA)])

    converter = LLAMA / "convert_lora_to_gguf.py"
    base = [sys.executable, str(converter), str(adapter), "--outfile", str(OUTFILE)]
    attempt = base + ["--outtype", "f16", "--base-model-id", BASE_MODEL_ID]
    if run(attempt, check=False).returncode != 0:
        print("не вышло с --base-model-id, пробую без него", flush=True)
        run(base + ["--outtype", "f16"])
    if not OUTFILE.exists():
        raise SystemExit("GGUF не создан")

    from gguf import GGUFReader

    reader = GGUFReader(str(OUTFILE))
    print("тензоров в GGUF:", len(reader.tensors), flush=True)
    print("размер, МБ:", round(OUTFILE.stat().st_size / 1024 / 1024, 1), flush=True)
    shutil.rmtree(LLAMA, ignore_errors=True)
    print("готово:", OUTFILE, flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        text = traceback.format_exc()
        (WORK / "error.txt").write_text(text, encoding="utf-8")
        print(text, file=sys.stderr, flush=True)
        raise
