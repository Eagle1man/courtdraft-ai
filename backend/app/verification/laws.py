"""База законодательства и расчёт государственной пошлины.

Проверка ссылок на нормы должна идти по базе законодательства, а не по
материалам дела. Суд ссылается на ст. 309 ГК РФ — этой статьи в иске
нет, и требовать её подтверждения материалами нельзя: так каждая
ссылка на норму помечалась неподтверждённой, включая верные.

Разница между двумя проверками:

* норма существует в кодексе и действует на дату дела — проверка по
  базе законодательства;
* утверждение подтверждено документом дела — проверка по материалам.

Первая ловит выдуманные и недействующие нормы, вторая — выдуманные
факты. Смешивать их нельзя: это разные вопросы.

База лежит в data/laws в виде JSON по кодексам. Она не полная — в ней
нормы, которые продукт применяет сам и которые чаще всего называет
модель. Новая норма добавляется записью в файл, код менять не нужно.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

#: Коды, которые продукт применяет сам и которые модель называет чаще
#: всего. Список закрытый намеренно: норма вне списка не проверяется,
#: а не признаётся несуществующей.
KNOWN_CODES = ("ГПК", "ГК", "НК", "КоАП", "АПК", "УК", "ТК", "СК", "КАС")

#: Имя файла базы — латиницей, код кодекса — кириллицей. Без этой
#: таблицы gpk.json загружался бы как код «GPK» и не находился при
#: проверке ссылки на «ГПК».
FILE_CODES: dict[str, str] = {
    "GPK": "ГПК",
    "GKK": "ГК",
    "NK": "НК",
    "KOAP": "КоАП",
    "APK": "АПК",
    "UK": "УК",
    "TK": "ТК",
    "SK": "СК",
    "KAS": "КАС",
}

#: Статьи, которые система цитирует сама по закону. Их подтверждение
#: материалами не требуется: в иске судью ст. 198 ГПК не пишут.
SYSTEM_ARTICLES: dict[str, set[str]] = {
    "ГПК": {"198", "199", "233", "321", "98", "103", "91", "196", "33", "23"},
    "ГК": {"309", "310", "330", "333", "393", "395"},
    "НК": {"333.19", "333.40"},
}


@dataclass(frozen=True)
class Norm:
    """Одна статья кодекса."""

    code: str
    article: str
    title: str = ""
    valid_from: date | None = None
    valid_to: date | None = None

    def is_in_force(self, on: date | None) -> bool:
        """Действует ли статья на указанную дату.

        Дата не передана — проверяем по сегодняшнему дню. Отсутствие
        даты в записи означает, что норма действует всегда: для
        процессуальных статей это верно, для материальных — нет, но
        угадывать дату вступления в силу мы не можем.
        """
        moment = on or date.today()
        if self.valid_from and moment < self.valid_from:
            return False
        return not (self.valid_to and moment > self.valid_to)


@dataclass
class LawRegistry:
    """Нормы по кодексам, загруженные из data/laws."""

    norms: dict[str, dict[str, Norm]] = field(default_factory=dict)
    source: str = ""

    def add(self, norm: Norm) -> None:
        self.norms.setdefault(norm.code, {})[norm.article] = norm

    def contains(self, code: str, article: str) -> bool:
        """Есть ли такая статья в кодексе."""
        return article in self.norms.get(code, {})

    def in_force(self, code: str, article: str, on: date | None = None) -> bool:
        """Существует ли статья и действует ли она на дату."""
        norm = self.norms.get(code, {}).get(article)
        if norm is None:
            return False
        return norm.is_in_force(on)

    def missing(self, code: str, article: str) -> str:
        """Почему норма не подтверждена — для текста замечания."""
        if not self.contains(code, article):
            return f"ст. {article} {code} отсутствует в базе законодательства"
        norm = self.norms[code][article]
        if not norm.is_in_force(date.today()):
            return (
                f"ст. {article} {code} не действует на дату дела "
                f"(действует с {norm.valid_from}, по {norm.valid_to})"
            )
        return ""

    @classmethod
    def load(cls, directory: Path) -> LawRegistry:
        """Загрузить нормы из JSON-файлов каталога.

        Файл называется по кодексу: gpk.json, gkk.json. Внутри — список
        статей с номером, названием и границами действия.
        """
        registry = cls(source=str(directory))
        if not directory.exists():
            return registry
        for path in sorted(directory.glob("*.json")):
            code = FILE_CODES.get(path.stem.upper(), path.stem.upper())
            if code not in KNOWN_CODES:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in data.get("articles", []):
                registry.add(
                    Norm(
                        code=code,
                        article=str(item["article"]),
                        title=str(item.get("title", "")),
                        valid_from=_parse_date(item.get("valid_from")),
                        valid_to=_parse_date(item.get("valid_to")),
                    )
                )
        return registry


def _parse_date(raw: Any) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


#: Тариф госпошлины по имущественным искам, подлежащим оценке —
#: НК РФ, ст. 333.19, п. 1, пп. 1. Диапазоны по цене иска сменяются
#: законом, поэтому редакции с датами действия: расчет от цены иска
#: ведется той редакцией, которая действовала на дату обращения в суд.
#: Новая редакция (259-ФЗ от 08.08.2024) проверена по consultant.ru и
#: zakonrf.info 2026-10-19; прежняя (198-ФЗ) — по странице изменений
#: того же источника; самая старая сетка — таблица ст. 333.19 редакции
#: 2001 года (до 374-ФЗ). Пример рецензента: цена иска 150 000 → 5 500 руб.
#: Граница новой редакции взята по п. 28 ст. 19 ФЗ-259: он применяется к
#: искам, направленным в суд после 08.09.2024, — иск от 08.09.2024 считается
#: прежней сеткой, с 09.09.2024 — новой.
@dataclass(frozen=True)
class _Band:
    """Диапазон цены иска с формулой пошлины внутри него."""

    top: float | None  # верхняя граница, None — до максимума
    base: float  # фиксированная часть
    rate: float  # ставка на превышение предыдущей границы
    floor: float = 0.0  # минимум внутри диапазона
    cap: float | None = None  # максимум сверху


#: Иски с 09.09.2024 — новая сетка (п. 28 ст. 19 ФЗ-259, см. комментарий выше).
_NEW_FEE_EDITION = date(2024, 9, 9)
#: До 29.01.2010 действовала сетка редакции 2001 года (374-ФЗ вступил в силу 29.01.2010).
_PRE2010_EDITION_END = date(2010, 1, 29)
_NEW_TARIFF: tuple[_Band, ...] = (
    _Band(100_000, 4_000, 0.0),
    _Band(300_000, 4_000, 0.03),
    _Band(500_000, 10_000, 0.025),
    _Band(1_000_000, 15_000, 0.02),
    _Band(3_000_000, 25_000, 0.01),
    _Band(8_000_000, 45_000, 0.007),
    _Band(24_000_000, 80_000, 0.0035),
    _Band(50_000_000, 136_000, 0.003),
    _Band(100_000_000, 214_000, 0.002),
    _Band(None, 314_000, 0.0015, cap=900_000),
)
_OLD_TARIFF: tuple[_Band, ...] = (
    _Band(20_000, 0.0, 0.04, floor=400.0),
    _Band(100_000, 800, 0.03),
    _Band(200_000, 3_200, 0.02),
    _Band(1_000_000, 5_200, 0.01),
    _Band(None, 13_200, 0.005, cap=60_000),
)
#: Редакция 2001 года: 4% (не менее 200), далее 400 + 3%, 1 600 + 2%,
#: 2 600 + 1%, 6 600 + 0,5% с потолком 20 000 руб.
_PRE2010_TARIFF: tuple[_Band, ...] = (
    _Band(10_000, 0.0, 0.04, floor=200.0),
    _Band(50_000, 400, 0.03),
    _Band(100_000, 1_600, 0.02),
    _Band(500_000, 2_600, 0.01),
    _Band(None, 6_600, 0.005, cap=20_000),
)

#: Судебный приказ — половина от пошлины имущественного иска
#: (ст. 333.19, п. 1, пп. 2). Формула одна, редакции меняют только
#: ставку, поэтому долю можно держать константой.
ORDER_FEE_RATE = 0.5

#: Иск имущественный, не подлежащий оценке / неимущественный,
#: для физических лиц (ст. 333.19, п. 1, пп. 3).
NON_PROPERTY_FEE_PHYSICAL = 3_000


def _fee_by_bands(bands: tuple[_Band, ...], price: float) -> float:
    """Пошлина по тарифной сетке одной редакции."""
    if price <= 0:
        return 0.0
    previous = 0.0
    for band in bands:
        if band.top is None or price <= band.top:
            fee = band.base + band.rate * (price - previous)
            if band.cap is not None:
                fee = min(fee, band.cap)
            return max(fee, band.floor)
        previous = band.top
    return 0.0


def state_fee(price: float, on: date | None = None) -> float:
    """Госпошлина по цене иска, НК РФ ст. 333.19.

    Дата обращения в суд выбирает редакцию: иски, направленные после
    08.09.2024, считаются по новой тарифной сетке; с 29.01.2010 по
    08.09.2024 — по прежней; раньше — по сетке 2001 года. Дата не
    передана — расчёт по действующей редакции, и это надо помнить:
    для архивных дел число будет не тем, каким платили истцы в тот год.
    """
    moment = on or date.today()
    if moment < _PRE2010_EDITION_END:
        return _fee_by_bands(_PRE2010_TARIFF, price)
    if moment < _NEW_FEE_EDITION:
        return _fee_by_bands(_OLD_TARIFF, price)
    return _fee_by_bands(_NEW_TARIFF, price)


def state_fee_order(price: float, on: date | None = None) -> float:
    """Пошлина по судебному приказу — половина от исковой."""
    return state_fee(price, on) * ORDER_FEE_RATE


def fee_matches(
    price: float, paid: float, on: date | None = None, tolerance: float = 1.0
) -> bool:
    """Сходится ли уплаченная пошлина с расчётной.

    Допуск в рубль: в делах пошлину иногда указывают с округлением,
    и требовать точного совпадения — ложные срабатывания.
    """
    return abs(state_fee(price, on) - paid) <= tolerance


def extract_fee(text: str) -> float | None:
    """Сумма пошлины из текста, если она названа."""
    match = re.search(
        r"(?:государственн\w*\s+)?пошлин\w*(?:\s+в\s+размере)?\s+"
        r"(\d[\d\s]*(?:,\d{1,2})?)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    digits = re.sub(r"[^\d,]", "", match.group(1))
    whole, _, frac = digits.partition(",")
    try:
        return float(whole or 0) + float((frac or "0").ljust(2, "0")[:2]) / 100
    except ValueError:
        return None
