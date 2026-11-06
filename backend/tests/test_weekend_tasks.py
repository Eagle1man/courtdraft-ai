"""Тесты выходного прогона: выборка документов, метрики, отчёт.

Сам прогон идёт на стенде часами, поэтому проверяется то, что можно
проверить быстро и что определяет смысл отчёта: выборка не должна
перекашиваться на один тип документов, перцентили должны считаться
честно, а отчёт — собираться из состояния, даже если часть задач упала.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.weekend.runner import build_report
from scripts.weekend.tasks import _percentile, pick_documents


class TestPickDocuments:
    def test_takes_evenly_from_each_type(self) -> None:
        rows = [
            ("1", "a.docx", "case", "CONTRACT", "text"),
            ("2", "b.docx", "case", "CONTRACT", "text"),
            ("3", "c.docx", "case", "CONTRACT", "text"),
            ("4", "d.docx", "case", "CLAIM", "text"),
            ("5", "e.docx", "case", "CLAIM", "text"),
        ]

        picked = pick_documents(rows, per_type=2, limit=10)

        assert [item[3] for item in picked].count("CONTRACT") == 2
        assert [item[3] for item in picked].count("CLAIM") == 2

    def test_limit_is_respected(self) -> None:
        rows = [(str(index), f"{index}.docx", "case", "CONTRACT", "text") for index in range(50)]

        picked = pick_documents(rows, per_type=10, limit=5)

        assert len(picked) == 5

    def test_unknown_type_is_not_lost(self) -> None:
        rows = [("1", "a.docx", "case", "", "text")]

        picked = pick_documents(rows, per_type=1, limit=10)

        assert len(picked) == 1
        assert picked[0][3] == ""

    def test_empty_input(self) -> None:
        assert pick_documents([]) == []


class TestPercentile:
    def test_empty_is_zero(self) -> None:
        assert _percentile([], 0.95) == 0.0

    def test_p50_of_even_list(self) -> None:
        assert _percentile([1.0, 2.0, 3.0, 4.0], 0.5) in {2.0, 3.0}

    def test_p95_is_near_max(self) -> None:
        values = [float(index) for index in range(100)]

        assert _percentile(values, 0.95) >= 90.0

    def test_single_value(self) -> None:
        assert _percentile([7.5], 0.95) == 7.5


class TestBuildReport:
    def test_report_is_written_even_with_failed_task(self, tmp_path: Path) -> None:
        state = {
            "tasks": {
                "tests": {
                    "status": "done",
                    "minutes": 12.5,
                    "result": {
                        "pytest_code": 0,
                        "pytest_tail": ["1319 passed"],
                        "ruff_code": 0,
                        "ruff_tail": ["All checks passed"],
                    },
                },
                "perf": {
                    "status": "failed",
                    "minutes": 0.3,
                    "result": {"error": "нет связи"},
                },
            }
        }

        build_report(tmp_path, state)

        text = (tmp_path / "report.md").read_text(encoding="utf-8")
        assert "Выходной прогон" in text
        assert "1319 passed" in text
        assert "**failed**" in text
        assert json.loads((tmp_path / "report.json").read_text(encoding="utf-8")) == state

    def test_extract_bench_section_has_numbers(self, tmp_path: Path) -> None:
        state = {
            "tasks": {
                "extract_bench": {
                    "status": "done",
                    "minutes": 40,
                    "result": {
                        "model": "qwen2.5:1.5b",
                        "documents": 12,
                        "ok": 9,
                        "needs_review": 3,
                        "timeouts": 0,
                        "amounts": 17,
                        "dates": 22,
                        "claims": 5,
                        "quotes_exact": 40,
                        "quotes_absent": 2,
                        "avg_seconds": 28.4,
                    },
                }
            }
        }

        build_report(tmp_path, state)

        text = (tmp_path / "report.md").read_text(encoding="utf-8")
        assert "qwen2.5:1.5b" in text
        assert "точных 40" in text
        assert "28.4" in text
