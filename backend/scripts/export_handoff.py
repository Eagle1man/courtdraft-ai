"""Собрать снимок стенда для работы без доступа к ВМ.

Что попадает в архив: консистентная копия базы, загруженные документы,
тексты кодексов, индекс норм, журналы службы и Ollama, а также отчёт
`report.md` — что за тест был, какие проекты сформированы, какие ошибки
и замечания найдены.

Зачем снимок, а не просто файлы: дома нужно понимать, что именно
сломалось, не имея под рукой стенда. Отчёт строится из базы, а не
пишется руками, поэтому его цифры можно перепроверить.

Запуск на стенде:
    .venv/bin/python -m scripts.export_handoff
    .venv/bin/python -m scripts.export_handoff --out ~/courtdraft-export
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from collections import Counter, defaultdict
from datetime import UTC, datetime

sys.stdout.reconfigure(errors="replace")

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "cases" / "courtdraft.db"
CASES_DIR = REPO_ROOT / "data" / "cases"
LAWS_DIR = REPO_ROOT / "backend" / "data" / "laws" / "sources"
QDRANT_DIR = REPO_ROOT / "data" / "qdrant"
DEFAULT_OUT = pathlib.Path.home() / "courtdraft-export"


def _backup_database(source: pathlib.Path, target: pathlib.Path) -> None:
    """Копия базы через sqlite backup: WAL-файл не копируется руками."""
    origin = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination = sqlite3.connect(target)
    try:
        with destination:
            origin.backup(destination)
    finally:
        destination.close()
        origin.close()


def _journal(service: str, target: pathlib.Path) -> int:
    """Выгрузить журнал службы целиком, если journalctl доступен."""
    try:
        result = subprocess.run(  # noqa: S603 - системная команда без внешних данных
            ["/usr/bin/journalctl", "-u", service, "--no-pager", "-o", "short-iso"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        target.write_text(f"журнал недоступен: {exc}\n", encoding="utf-8")
        return 0
    target.write_text(result.stdout, encoding="utf-8")
    return len(result.stdout.splitlines())


def _load_rows(connection: sqlite3.Connection, query: str, params: tuple = ()) -> list[dict]:
    connection.row_factory = sqlite3.Row
    return [dict(row) for row in connection.execute(query, params)]


def _duration_seconds(row: dict) -> float | None:
    if not row.get("started_at") or not row.get("finished_at"):
        return None
    try:
        start = datetime.fromisoformat(str(row["started_at"]))
        finish = datetime.fromisoformat(str(row["finished_at"]))
    except ValueError:
        return None
    return round((finish - start).total_seconds(), 1)


def _report(db_path: pathlib.Path, journal_path: pathlib.Path, target: pathlib.Path) -> None:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cases = _load_rows(connection, "select * from cases")
        drafts = _load_rows(connection, "select * from drafts")
        versions = _load_rows(connection, "select * from draft_versions")
        checks = _load_rows(connection, "select * from verifications")
        jobs = _load_rows(connection, "select * from jobs")
        documents = _load_rows(
            connection, "select case_id, count(*) n from case_documents group by case_id"
        )
    finally:
        connection.close()

    docs_by_case = {row["case_id"]: row["n"] for row in documents}
    case_by_id = {row["id"]: row for row in cases}
    version_by_draft: dict[str, list[dict]] = defaultdict(list)
    for version in versions:
        version_by_draft[version["draft_id"]].append(version)

    issue_types: Counter[str] = Counter()
    severity: Counter[str] = Counter()
    invented: list[str] = []
    for check in checks:
        try:
            issues = json.loads(check.get("issues") or "[]")
        except json.JSONDecodeError:
            continue
        for issue in issues:
            kind = str(issue.get("type", "?"))
            issue_types[kind] += 1
            severity[str(issue.get("severity", "?"))] += 1
            if kind == "UNSUPPORTED_CLAIM" and len(invented) < 20:
                invented.append(str(issue.get("reason", ""))[:160])

    lines: list[str] = []
    lines.append("# Снимок стенда CourtDraft AI")
    lines.append("")
    lines.append(f"Собран: {datetime.now(UTC).strftime('%d.%m.%Y %H:%M UTC')}")
    lines.append("")
    lines.append("## Что внутри архива")
    lines.append("")
    lines.append("| Путь | Что это |")
    lines.append("|---|---|")
    lines.append("| `courtdraft.db` | база на момент снимка (дела, проекты, версии, проверки) |")
    lines.append("| `cases/originals/` | загруженные участниками документы |")
    lines.append("| `laws/` | тексты ГК, ГПК, АПК (JSONL) |")
    lines.append("| `qdrant/` | индекс норм, собранный `bge-m3` |")
    lines.append("| `logs/backend.log` | журнал службы: запросы, ошибки, перезапуски |")
    lines.append("| `logs/ollama.log` | журнал Ollama: скорость, загрузка моделей |")
    lines.append("")
    lines.append("## Итоги теста")
    lines.append("")
    lines.append(f"- дел: **{len(cases)}**")
    lines.append(f"- проектов: **{len(drafts)}**, версий: **{len(versions)}**")
    human = sum(1 for item in versions if item.get("author") == "user")
    lines.append(f"- версий, отредактированных человеком: **{human}**")
    lines.append(f"- проверок: **{len(checks)}**")
    by_state = Counter(item.get("state") for item in jobs)
    lines.append(
        "- заданий: "
        + ", ".join(f"{state}: {count}" for state, count in sorted(by_state.items()))
    )
    lines.append("")

    failed = [item for item in jobs if item.get("state") == "FAILED"]
    if failed:
        lines.append("### Проваленные задания")
        lines.append("")
        for item in failed[:15]:
            lines.append(f"- {item.get('created_at')}: {str(item.get('error'))[:200]}")
        lines.append("")

    lines.append("### Скорость формирования проектов")
    lines.append("")
    lines.append("| Дело | Задание | Секунд |")
    lines.append("|---|---|---|")
    for job in sorted(jobs, key=lambda item: str(item.get("created_at")), reverse=True)[:15]:
        seconds = _duration_seconds(job) if _duration_seconds(job) is not None else "—"
        case = case_by_id.get(job.get("case_id"), {})
        lines.append(f"| {case.get('case_number', '—')} | {job.get('state')} | {seconds} |")
    lines.append("")

    lines.append("## Замечания проверки")
    lines.append("")
    lines.append("| Тип | Сколько |")
    lines.append("|---|---|")
    for kind, count in issue_types.most_common():
        lines.append(f"| {kind} | {count} |")
    lines.append("")
    severity_text = ", ".join(f"{key}: {value}" for key, value in severity.most_common())
    lines.append(f"По важности: {severity_text}")
    lines.append("")
    if invented:
        lines.append("### Примеры утверждений без источника в материалах")
        lines.append("")
        for text in invented:
            lines.append(f"- {text}")
        lines.append("")

    lines.append("## Дела")
    lines.append("")
    lines.append("| № | Тип | Документов | Версий | Проверка | Замечаний |")
    lines.append("|---|---|---|---|---|---|")
    for draft in sorted(drafts, key=lambda item: str(item.get("updated_at")), reverse=True):
        case = case_by_id.get(draft.get("case_id"), {})
        draft_versions = version_by_draft.get(draft.get("id"), [])
        last_check = next(
            (check for check in checks if check.get("draft_id") == draft.get("id")), None
        )
        issues = 0
        status = "—"
        if last_check is not None:
            status = str(last_check.get("status"))
            try:
                issues = len(json.loads(last_check.get("issues") or "[]"))
            except json.JSONDecodeError:
                issues = 0
        lines.append(
            f"| {case.get('case_number', '—')} | {case.get('case_type', '—')} | "
            f"{docs_by_case.get(draft.get('case_id'), 0)} | {len(draft_versions)} | "
            f"{status} | {issues} |"
        )
    lines.append("")

    lines.append("## Ошибки в журнале")
    lines.append("")
    if journal_path.exists():
        error_lines = [
            line
            for line in journal_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if "ERROR" in line or "Traceback" in line or "database is locked" in line
        ]
        codes = Counter()
        for line in journal_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if " 500 " in line:
                codes["500"] += 1
            elif " 409 " in line:
                codes["409"] += 1
            elif " 404 " in line:
                codes["404"] += 1
        lines.append(f"- строк с ошибками: **{len(error_lines)}**")
        answers = (
            f"500: {codes.get('500', 0)}, 409: {codes.get('409', 0)}, "
            f"404: {codes.get('404', 0)}"
        )
        lines.append(f"- ответов с ошибкой: **{answers}**")
        lines.append("")
        for line in error_lines[-15:]:
            lines.append(f"    {line[:220]}")
    else:
        lines.append("журнал не собран")
    lines.append("")

    lines.append("## Как продолжить дома")
    lines.append("")
    lines.append("```bash")
    lines.append("# 1. База рядом с проектом (сначала остановить службу, чтобы не было WAL)")
    lines.append("cp courtdraft.db <проект>/data/cases/courtdraft.db")
    lines.append("# 2. Документы участников")
    lines.append("cp -r cases/originals/* <проект>/data/cases/originals/")
    lines.append("# 3. Индекс норм (если есть bge-m3; иначе пересобрать)")
    lines.append("cp -r qdrant <проект>/data/qdrant")
    lines.append("```")
    lines.append("")
    lines.append("Для дообучения: пары собираются скриптом `scripts/dataset/export_cases.py`")
    lines.append("из версий, отредактированных человеком (`author = 'user'`).")

    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _decisions(db_path: pathlib.Path, target: pathlib.Path) -> int:
    """Выгрузить сами проекты решений отдельными файлами.

    В базе текст каждой версии есть, но читать его оттуда неудобно.
    Отдельный файл на дело — это то, что можно открыть, распечатать и
    разобрать без программы.
    """
    target.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    written = 0
    try:
        drafts = connection.execute("select * from drafts").fetchall()
        for draft in drafts:
            case = connection.execute(
                "select * from cases where id = ?", (draft["case_id"],)
            ).fetchone()
            version = connection.execute(
                "select * from draft_versions where draft_id = ? order by version desc limit 1",
                (draft["id"],),
            ).fetchone()
            if case is None or version is None:
                continue
            check = connection.execute(
                "select * from verifications where draft_id = ? order by created_at desc limit 1",
                (draft["id"],),
            ).fetchone()

            issues: list[dict] = []
            if check is not None:
                try:
                    issues = json.loads(check["issues"] or "[]")
                except json.JSONDecodeError:
                    issues = []

            number = str(case["case_number"] or case["id"])
            safe = "".join(
                symbol if symbol.isalnum() or symbol in "-_." else "_" for symbol in number
            )
            path = target / f"{safe}-v{version['version']}.md"

            header = [
                f"# Дело № {number}",
                "",
                f"- вид судопроизводства: {case['case_type'] or '—'}",
                f"- версия: {version['version']}, автор: {version['author']}",
                f"- проверка: {check['status'] if check is not None else '—'}",
                f"- замечаний: {len(issues)}",
                "",
            ]
            if issues:
                header.append("## Замечания проверки")
                header.append("")
                for issue in issues:
                    header.append(
                        f"- [{issue.get('severity')}] {issue.get('type')} "
                        f"({issue.get('section') or '—'}): {issue.get('reason')}"
                    )
                header.append("")
            header.append("## Текст проекта")
            header.append("")
            path.write_text(
                "\n".join(header) + str(version["text"] or "") + "\n", encoding="utf-8"
            )
            written += 1
    finally:
        connection.close()
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    parser.add_argument("--db", type=pathlib.Path, default=DEFAULT_DB)
    parser.add_argument("--no-qdrant", action="store_true", help="не класть индекс норм")
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"Нет базы {args.db}")

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    work = args.out / f"handoff-{stamp}"
    if work.exists():
        shutil.rmtree(work)
    (work / "logs").mkdir(parents=True)
    (work / "cases").mkdir(parents=True)

    print(f"снимок: {work}")
    _backup_database(args.db, work / "courtdraft.db")
    print(f"база: {(work / 'courtdraft.db').stat().st_size / 1024 / 1024:.1f} МБ")

    originals = CASES_DIR / "originals"
    if originals.exists():
        shutil.copytree(originals, work / "cases" / "originals")
        print(f"документы: {sum(1 for _ in (work / 'cases').rglob('*') if _.is_file())} файлов")

    if LAWS_DIR.exists():
        shutil.copytree(LAWS_DIR, work / "laws")
        print(f"кодексы: {len(list((work / 'laws').glob('*.jsonl')))} файлов")

    if not args.no_qdrant and QDRANT_DIR.exists():
        shutil.copytree(QDRANT_DIR, work / "qdrant")
        print(f"индекс норм: {sum(1 for _ in (work / 'qdrant').rglob('*') if _.is_file())} файлов")

    backend_lines = _journal("courtdraft-backend", work / "logs" / "backend.log")
    ollama_lines = _journal("ollama", work / "logs" / "ollama.log")
    print(f"журналы: backend {backend_lines} строк, ollama {ollama_lines} строк")

    _report(work / "courtdraft.db", work / "logs" / "backend.log", work / "report.md")
    print("отчёт: report.md")

    decisions = _decisions(work / "courtdraft.db", work / "decisions")
    print(f"проекты решений: {decisions} файлов")

    archive = args.out / f"courtdraft-handoff-{stamp}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(work, arcname=work.name)
    size = archive.stat().st_size / 1024 / 1024
    print(f"архив: {archive} ({size:.0f} МБ)")
    shutil.rmtree(work)


if __name__ == "__main__":
    main()
