"""Нарезка реальных обезличенных абзацев: что попадает и что отсеивается."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "make_real_benign_fragments.py"

spec = importlib.util.spec_from_file_location("make_real_benign_fragments", SCRIPT)
assert spec and spec.loader
fragments = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fragments
spec.loader.exec_module(fragments)

VALID = (
    "Суд, рассмотрев материалы дела, установил, что между сторонами заключён "
    "договор поставки, обязательства по оплате которого не исполнены в срок. "
    "Задолженность подтверждена актом сверки и платёжными документами."
)
REQUISITES = (
    "ИНН 7701234567, ОГРН 1027700132195, расчётный счёт в банке, адрес: "
    "г. Москва, ул. Тверская, д. 1, телефон и факс для связи с организацией."
)


class TestCollect:
    def test_keeps_russian_paragraph_and_drops_requisites(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "case.txt"
        path.write_text(f"{VALID}\n\n{REQUISITES}\n", encoding="utf-8")
        found = fragments.collect(tmp_path, min_len=150, max_len=600)
        texts = [text for text, _name in found]
        assert any("договор поставки" in text for text in texts)
        assert not any("ИНН" in text for text in texts)

    def test_skips_english(self, tmp_path: Path) -> None:
        path = tmp_path / "case.txt"
        path.write_text("The court examined the case materials and decided. " * 5, encoding="utf-8")
        assert fragments.collect(tmp_path, min_len=150, max_len=600) == []

    def test_respects_length_bounds(self, tmp_path: Path) -> None:
        short = "Суд установил факт."
        path = tmp_path / "case.txt"
        path.write_text(f"{short}\n\n{VALID}\n", encoding="utf-8")
        found = fragments.collect(tmp_path, min_len=150, max_len=600)
        assert [text for text, _ in found] == [fragments.clean(VALID)]


class TestPick:
    def test_deduplicates_and_limits(self) -> None:
        items = [(f"Уникальный абзац номер {index} о взыскании долга.", "f") for index in range(10)]
        items.append((items[0][0], "f"))
        picked = fragments.pick(items, limit=5)
        assert len(picked) == 5
        assert len({text for text, _ in picked}) == 5
