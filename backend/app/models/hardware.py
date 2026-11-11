"""Профиль железа и рекомендация локальной модели.

Правила — ориентиры, а не гарантия: скорость зависит от CPU, диска и
квантования. Поэтому рекомендация всегда содержит предупреждение, а не
обещание «будет быстро».
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class HardwareProfile:
    ram_gb: float
    cpu_cores: int
    disk_free_gb: float
    gpu_name: str = ""
    vram_gb: float = 0.0
    has_internet: bool = False


@dataclass(frozen=True, slots=True)
class Recommendation:
    fast_model: str
    quality_model: str
    api_model: str
    notes: list[str] = field(default_factory=list)


FAST_MODEL = "qwen2.5:1.5b"
QUALITY_7B = "qwen2.5:7b"
QUALITY_14B = "qwen2.5:14b"
QUALITY_32B = "qwen2.5:32b"
API_MODEL = "auto:deepseek-flash"


def recommend(profile: HardwareProfile) -> Recommendation:
    """Выбрать быструю и качественную модель под профиль."""
    notes: list[str] = []

    if profile.ram_gb < 6:
        notes.append(
            "RAM меньше 6 ГБ: работа возможна только на 1.5B и может "
            "упираться в память при больших документах."
        )
    if profile.disk_free_gb < 2:
        notes.append("Свободного места меньше 2 ГБ: модель 1.5B может не поместиться.")

    vram = profile.vram_gb
    if vram >= 20:
        quality = QUALITY_32B
        needed = 22.0
        notes.append("GPU 20+ ГБ: 32B Q4, ожидаемо 3–6 мин на проект.")
    elif vram >= 10:
        quality = QUALITY_14B
        needed = 11.0
        notes.append("GPU 10–12 ГБ: 14B Q4, ожидаемо 2–4 мин на проект.")
    elif vram >= 6:
        quality = QUALITY_7B
        needed = 6.0
        notes.append("GPU 6–8 ГБ: 7B Q4, ожидаемо 1–3 мин на проект.")
    elif profile.ram_gb >= 12:
        quality = QUALITY_7B
        needed = 6.0
        notes.append(
            "GPU нет, RAM 12+ ГБ: 7B Q4 на CPU, ожидаемо 20–30 мин на проект."
        )
    elif profile.ram_gb >= 8:
        quality = QUALITY_7B
        needed = 6.0
        notes.append(
            "GPU нет, RAM 8–11 ГБ: 7B Q4 на CPU, медленно (20–30 мин) "
            "и может не хватить памяти при больших документах."
        )
    else:
        quality = FAST_MODEL
        needed = 2.0
        notes.append(
            "Меньше 8 ГБ RAM: качественная 7B не рекомендуется, "
            "остаётся 1.5B."
        )

    if profile.disk_free_gb < needed:
        notes.append(
            f"Для выбранной качественной модели нужно ~{needed:.0f} ГБ, "
            f"свободно {profile.disk_free_gb:.1f} ГБ."
        )

    if profile.has_internet:
        notes.append(
            "Интернет есть: доступен DeepSeek Flash через ZvenoAI "
            "(быстро, но текст уходит в облако)."
        )
    else:
        notes.append(
            "Интернета нет: только локальная модель; ключ ZvenoAI не поможет."
        )

    if profile.gpu_name:
        notes.append(f"GPU: {profile.gpu_name}, VRAM {profile.vram_gb:.1f} ГБ.")
    else:
        notes.append("GPU не обнаружен: рекомендация для CPU.")

    return Recommendation(
        fast_model=FAST_MODEL,
        quality_model=quality,
        api_model=API_MODEL,
        notes=notes,
    )
