"""Слить LoRA-адаптер с базовой моделью и собрать GGUF Q4_K_M для Ollama.

Запускается отдельным Kaggle-ядром после обучения: адаптер монтируется
как выход предыдущего ядра (`kernel_sources`), поэтому качать его на
машину и заливать обратно не нужно.

Почему не unsloth: на этом образе он конфликтует по версиям (trl/torch),
а конвертация — это обычные шаги llama.cpp, которые от unsloth не зависят.
Слияние делается через peft, конвертация — `convert_hf_to_gguf.py`,
квантование — собранным `llama-quantize`.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import traceback

BASE = "Qwen/Qwen2.5-1.5B-Instruct"
WORK = pathlib.Path("/kaggle/working")
MERGED = WORK / "merged"
LLAMA = WORK / "llama.cpp"


def run(command: list[str]) -> None:
    print("+", " ".join(str(part) for part in command), flush=True)
    subprocess.run(command, check=True)


def find_adapter() -> pathlib.Path:
    print("содержимое /kaggle/input:", flush=True)
    for root in sorted(pathlib.Path("/kaggle/input").iterdir()):
        print(" ", root, flush=True)
        if root.is_dir():
            for child in sorted(root.iterdir()):
                print("    ", child.name, flush=True)
    candidates = list(pathlib.Path("/kaggle/input").rglob("adapter_config.json"))
    if not candidates:
        raise SystemExit("адаптер не найден: нет adapter_config.json")
    print("адаптер найден:", candidates[0].parent, flush=True)
    return candidates[0].parent


def main() -> None:
    adapter = find_adapter()

    run([sys.executable, "-m", "pip", "install", "-q", "gguf", "sentencepiece"])
    run([sys.executable, "-m", "pip", "install", "-q", "-U", "peft", "accelerate"])

    merge = f'''
import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer

base = AutoModelForCausalLM.from_pretrained(
    "{BASE}", torch_dtype=torch.float16, device_map="cpu", trust_remote_code=True
)
model = PeftModel.from_pretrained(base, "{adapter}")
model = model.merge_and_unload()
model.save_pretrained("{MERGED}", safe_serialization=True)
AutoTokenizer.from_pretrained("{BASE}", trust_remote_code=True).save_pretrained("{MERGED}")
print("слияние готово")
'''
    run([sys.executable, "-c", merge])

    run(["git", "clone", "--depth", "1", "https://github.com/ggml-org/llama.cpp", str(LLAMA)])
    run(
        [
            sys.executable,
            str(LLAMA / "convert_hf_to_gguf.py"),
            str(MERGED),
            "--outfile",
            str(WORK / "model-f16.gguf"),
            "--outtype",
            "f16",
        ]
    )

    run(["cmake", "-S", str(LLAMA), "-B", str(LLAMA / "build"), "-DLLAMA_CURL=OFF"])
    run(
        [
            "cmake",
            "--build",
            str(LLAMA / "build"),
            "--target",
            "llama-quantize",
            "-j",
            "4",
        ]
    )
    run(
        [
            str(LLAMA / "build" / "bin" / "llama-quantize"),
            str(WORK / "model-f16.gguf"),
            str(WORK / "model-q4_k_m.gguf"),
            "Q4_K_M",
        ]
    )

    (WORK / "model-f16.gguf").unlink(missing_ok=True)
    print("готово, файлы:", flush=True)
    for path in sorted(WORK.glob("*.gguf")):
        print(" ", path.name, round(path.stat().st_size / 1024 / 1024, 1), "МБ", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        text = traceback.format_exc()
        (WORK / "error.txt").write_text(text, encoding="utf-8")
        print(text, file=sys.stderr, flush=True)
        raise
