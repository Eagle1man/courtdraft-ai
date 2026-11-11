"""Рекомендация модели по профилю железа."""
from __future__ import annotations

from app.models.hardware import (
    FAST_MODEL,
    QUALITY_7B,
    QUALITY_14B,
    QUALITY_32B,
    HardwareProfile,
    recommend,
)


def test_weak_machine_gets_fast_model_everywhere() -> None:
    result = recommend(
        HardwareProfile(ram_gb=4, cpu_cores=2, disk_free_gb=20)
    )
    assert result.fast_model == FAST_MODEL
    assert result.quality_model == FAST_MODEL


def test_cpu_16gb_gets_7b_as_quality() -> None:
    result = recommend(
        HardwareProfile(ram_gb=16, cpu_cores=8, disk_free_gb=100)
    )
    assert result.quality_model == QUALITY_7B


def test_gpu_8gb_gets_7b() -> None:
    result = recommend(
        HardwareProfile(
            ram_gb=16,
            cpu_cores=8,
            disk_free_gb=100,
            gpu_name="RTX 3050",
            vram_gb=8,
        )
    )
    assert result.quality_model == QUALITY_7B


def test_gpu_12gb_gets_14b() -> None:
    result = recommend(
        HardwareProfile(
            ram_gb=32,
            cpu_cores=12,
            disk_free_gb=200,
            gpu_name="RTX 3060",
            vram_gb=12,
        )
    )
    assert result.quality_model == QUALITY_14B


def test_gpu_24gb_gets_32b() -> None:
    result = recommend(
        HardwareProfile(
            ram_gb=64,
            cpu_cores=16,
            disk_free_gb=500,
            gpu_name="RTX 4090",
            vram_gb=24,
        )
    )
    assert result.quality_model == QUALITY_32B


def test_small_disk_adds_warning() -> None:
    result = recommend(
        HardwareProfile(ram_gb=16, cpu_cores=8, disk_free_gb=3)
    )
    assert any("нужно" in note for note in result.notes)
