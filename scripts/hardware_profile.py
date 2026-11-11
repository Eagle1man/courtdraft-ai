"""Определить железо и рекомендовать модель.

Запуск:
    python scripts/hardware_profile.py
    python scripts/hardware_profile.py --json

Скрипт ничего не устанавливает и не меняет. Только читает.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.models.hardware import HardwareProfile, recommend


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def total_ram_gb() -> float:
    if os.name == "nt":
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(  # type: ignore[attr-defined]
            ctypes.byref(stat)
        ):
            return 0.0
        return stat.ullTotalPhys / 1024**3
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return pages * page_size / 1024**3
    except (AttributeError, ValueError, OSError):
        return 0.0


def gpu_info() -> tuple[str, float]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "", 0.0
    if result.returncode != 0 or not result.stdout.strip():
        return "", 0.0
    first = result.stdout.strip().splitlines()[0]
    parts = [part.strip() for part in first.split(",")]
    if len(parts) < 2:
        return first, 0.0
    try:
        vram = float(parts[1]) / 1024
    except ValueError:
        vram = 0.0
    return parts[0], vram


def has_internet() -> bool:
    try:
        with socket.create_connection(("api.zveno.ai", 443), timeout=3):
            return True
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    gpu_name, vram = gpu_info()
    profile = HardwareProfile(
        ram_gb=round(total_ram_gb(), 1),
        cpu_cores=os.cpu_count() or 1,
        disk_free_gb=round(shutil.disk_usage(ROOT).free / 1024**3, 1),
        gpu_name=gpu_name,
        vram_gb=round(vram, 1),
        has_internet=has_internet(),
    )
    result = recommend(profile)

    if args.json:
        print(
            json.dumps(
                {
                    "profile": {
                        "ram_gb": profile.ram_gb,
                        "cpu_cores": profile.cpu_cores,
                        "disk_free_gb": profile.disk_free_gb,
                        "gpu_name": profile.gpu_name,
                        "vram_gb": profile.vram_gb,
                        "has_internet": profile.has_internet,
                        "os": platform.platform(),
                    },
                    "recommendation": {
                        "fast_model": result.fast_model,
                        "quality_model": result.quality_model,
                        "api_model": result.api_model,
                        "notes": result.notes,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print("Профиль:")
    print(f"  RAM: {profile.ram_gb} ГБ")
    print(f"  Ядер CPU: {profile.cpu_cores}")
    print(f"  Свободно на диске: {profile.disk_free_gb} ГБ")
    print(f"  GPU: {profile.gpu_name or 'не обнаружен'}")
    print(f"  VRAM: {profile.vram_gb} ГБ")
    print(f"  Интернет: {'да' if profile.has_internet else 'нет'}")
    print()
    print(f"Быстро:      {result.fast_model}")
    print(f"Качественно: {result.quality_model}")
    print(f"API:         {result.api_model}")
    print()
    for note in result.notes:
        print(f"- {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
