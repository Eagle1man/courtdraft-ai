# -*- coding: utf-8 -*-
"""Собирает демонстрационную выборку расписания для site/schedule/.

Источник — выгрузка FET v3 (`data.json` в каталоге `out/site/<версия>`).
Страница статическая: данные кладутся рядом в `demo-data.json`, никакого
сервера не нужно.

Берутся несколько групп из разных академий и по три недели: этого хватает,
чтобы показать выбор группы, сетку, правку и публикацию, но страница
остаётся лёгкой.

Запуск:
    python scripts/build_schedule_demo.py [путь к data.json]
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "site" / "schedule" / "demo-data.json"
FET_V3_OUT = Path(os.environ.get("FETV3_OUT", r"D:\FET_v3\out"))

MAX_GROUPS_PER_FACULTY = 2
MAX_WEEKS_PER_GROUP = 3


def find_source() -> Path:
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    site_dir = FET_V3_OUT / "site"
    marker = site_dir / "latest.txt"
    if marker.exists():
        version = marker.read_text(encoding="utf-8").strip()
        candidate = site_dir / version / "data.json"
        if candidate.exists():
            return candidate
    candidates = sorted(site_dir.glob("*/data.json"))
    if not candidates:
        raise SystemExit(
            f"Не найдена выгрузка в {site_dir}. "
            "Сначала выполните `python run.py export` в D:\\FET_v3."
        )
    return candidates[-1]


def iso_from_site(date_text: str) -> str:
    return dt.datetime.strptime(date_text, "%d-%m-%Y").date().isoformat()


def choose_groups(groups: dict[str, dict[str, int]]) -> dict[str, str]:
    chosen: dict[str, str] = {}
    for faculty, items in groups.items():
        names = [name for name in sorted(items) if "," not in name]
        for name in names[:MAX_GROUPS_PER_FACULTY]:
            chosen[name] = faculty
    return chosen


def pick_weeks(weeks: dict[str, object]) -> list[str]:
    keys = sorted(weeks, key=lambda value: int(value))
    if len(keys) <= MAX_WEEKS_PER_GROUP:
        return keys
    middle = keys[len(keys) // 2]
    return [keys[0], middle, keys[-1]]


def build_dataset(source: Path) -> dict[str, object]:
    data = json.loads(source.read_text(encoding="utf-8"))
    groups: dict[str, dict[str, int]] = data["groups"]
    schedules: dict[str, object] = data["schedules"]
    chosen = choose_groups(groups)

    demo_groups: dict[str, dict[str, int]] = {}
    demo_schedules: dict[str, object] = {}
    lessons: list[dict[str, object]] = []
    counter = 0
    for group, faculty in chosen.items():
        schedule = schedules.get(group)
        if not schedule:
            continue
        label, weeks = schedule
        selected = pick_weeks(weeks)
        demo_weeks = {week: weeks[week] for week in selected}
        demo_schedules[group] = [label, demo_weeks]
        demo_groups.setdefault(faculty, {})[group] = 1
        for week, (meta, grid) in demo_weeks.items():
            for day, pairs in grid.items():
                for pair, items in pairs.items():
                    for lesson in items:
                        counter += 1
                        lessons.append(
                            {
                                "id": counter,
                                "period": meta["per"],
                                "week": week,
                                "source_row": 1000 + counter,
                                "date": iso_from_site(lesson["DATE"]),
                                "pair": int(pair),
                                "group": group,
                                "subgroup": lesson["SUBG"],
                                "subject": lesson["SUBJECT"],
                                "type": lesson["SUBJ_TYPE"],
                                "teacher": lesson["NAME"],
                                "room": lesson["AUD"],
                            }
                        )

    lessons.sort(key=lambda item: (item["date"], item["pair"], item["group"]))
    for index, lesson in enumerate(lessons, start=1):
        lesson["id"] = index

    return {
        "demo": True,
        "note": (
            "Демонстрационная выборка из реальной выгрузки AVTOR. "
            "Данные — открытое расписание вуза; изменения на странице "
            "не сохраняются."
        ),
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "school": data.get("school", "Южный университет (ИУБиП)"),
        "scope": data.get("scope", ""),
        "groups": demo_groups,
        "schedules": demo_schedules,
        "lessons": lessons,
        "runs": read_runs(),
        "conflicts": demo_conflicts(lessons),
        "versions": demo_versions(
            data,
            groups_count=sum(len(items) for items in demo_groups.values()),
            lessons_count=len(lessons),
        ),
    }


def read_runs() -> list[dict[str, object]]:
    """Реальные результаты последней сборки и проверки FET, если они есть."""
    runs: list[dict[str, object]] = []
    build_path = FET_V3_OUT / "build_summary.json"
    validation_path = FET_V3_OUT / "validation" / "summary.json"
    if not build_path.exists():
        return runs
    builds = json.loads(build_path.read_text(encoding="utf-8-sig")).get("builds", [])
    validation = {}
    if validation_path.exists():
        validation = json.loads(validation_path.read_text(encoding="utf-8-sig"))
    for item in builds:
        name = item["name"]
        check = validation.get(name, {})
        runs.append(
            {
                "name": name,
                "activities": item["stats"].get("activities", 0),
                "conflicts": len(item.get("conflicts", [])),
                "overloads": len(item.get("overloads", [])),
                "schema_ok": bool(check.get("static_ok")),
                "fet_ok": bool(check.get("run_executed")) and not check.get("fatal"),
                "broken_soft": check.get("broken_soft"),
            }
        )
    return runs


def demo_conflicts(lessons: list[dict[str, object]]) -> list[dict[str, object]]:
    """Два наглядных примера. Помечены demo=true, это не данные выгрузки."""
    if len(lessons) < 2:
        return []
    first = lessons[0]
    second = next(
        (item for item in lessons if item["group"] == first["group"] and item["id"] != first["id"]),
        lessons[1],
    )
    third = next((item for item in lessons if item["group"] != first["group"]), lessons[-1])
    return [
        {
            "demo": True,
            "kind": "student",
            "date": first["date"],
            "pair": first["pair"],
            "entity": first["group"],
            "detail": (
                f"«{first['subject']}» ({first['teacher']}) и "
                f"«{second['subject']}» ({second['teacher']}) — пример накладки группы"
            ),
        },
        {
            "demo": True,
            "kind": "room",
            "date": third["date"],
            "pair": third["pair"],
            "entity": third["room"],
            "detail": (
                f"Аудитория {third['room']}: «{third['subject']}» ({third['group']}) — "
                "пример накладки аудитории"
            ),
        },
    ]


def demo_versions(
    data: dict[str, object],
    *,
    groups_count: int,
    lessons_count: int,
) -> list[dict[str, object]]:
    """Демонстрационные версии выгрузки — с числами демо-выборки."""
    today = dt.date.today()
    return [
        {
            "version": f"{today.isoformat()}_1000",
            "generated": f"{today.isoformat()}T10:00:00",
            "scope": data.get("scope", ""),
            "groups": groups_count,
            "lessons": lessons_count,
            "current": True,
        },
        {
            "version": f"{(today - dt.timedelta(days=7)).isoformat()}_1830",
            "generated": f"{(today - dt.timedelta(days=7)).isoformat()}T18:30:00",
            "scope": data.get("scope", ""),
            "groups": groups_count,
            "lessons": lessons_count,
            "current": False,
        },
    ]


def main() -> int:
    source = find_source()
    dataset = build_dataset(source)
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(
        json.dumps(dataset, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    size = TARGET.stat().st_size
    print(f"Источник: {source}")
    print(f"Записано: {TARGET} ({size:,} байт)")
    print(
        f"Групп: {sum(len(items) for items in dataset['groups'].values())}, "
        f"занятий: {len(dataset['lessons'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
