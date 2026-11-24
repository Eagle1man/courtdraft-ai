"""Этап 0: найти реальный паспорт, валидаторы и генератор иска.

Пишет `artifacts/passport/passport_contract.json` и `ARCHITECTURE_AUDIT.md`.
Ничего не меняет в коде. Запуск: `python scripts/passport_audit.py [корень]`.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
OUT = ROOT / "artifacts" / "passport"
SKIP = {".git", ".venv", "node_modules", "__pycache__", "stage", "dist"}


def walk_files(suffixes: set[str]):
    for path in ROOT.rglob("*"):
        if (
            path.is_file()
            and path.suffix.lower() in suffixes
            and not any(part in SKIP for part in path.parts)
        ):
            yield path


def find_passport_samples() -> list[tuple[Path, dict]]:
    pattern = re.compile(r"(паспорт|passport)", re.I)
    output: list[tuple[Path, dict]] = []
    for path in walk_files({".json"}):
        if not (pattern.search(path.name) or re.search(r"^00-", path.name)):
            continue
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and len(data) >= 3:
            output.append((path, data))
    return output


def infer_type(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    text = str(value)
    if re.fullmatch(r"\d{1,2}[./]\d{1,2}[./]\d{2,4}", text):
        return "date"
    if re.fullmatch(r"-?\d[\d\s]*(?:[.,]\d+)?", text) and re.search(r"\d{2,}", text):
        return "money?"
    return "string"


def infer_schema(samples: list[tuple[Path, dict]]) -> dict:
    fields: dict[str, dict] = {}
    counts = len(samples)
    for _, data in samples:
        for key, value in data.items():
            field = fields.setdefault(
                key, {"types": set(), "required": 0, "examples": []}
            )
            field["types"].add(infer_type(value))
            field["required"] += 1
            if len(field["examples"]) < 2 and value not in (None, "", []):
                field["examples"].append(str(value)[:120])
    for field in fields.values():
        field["types"] = sorted(field["types"])
        field["required"] = field["required"] == counts
    return fields


ROLE_HINTS = [
    ("plaintiff_name", ["истец", "заявитель", "взыскатель"]),
    ("defendant_name", ["ответчик", "должник"]),
    ("plaintiff_address", ["адрес истца"]),
    ("defendant_address", ["адрес ответчика"]),
    ("plaintiff_inn", ["инн истца"]),
    ("defendant_inn", ["инн ответчика", "инн должника"]),
    ("contract_number", ["договор", "номер"]),
    ("contract_date", ["дата договора", "заключен"]),
    ("obligation_amount", ["сумма договора", "стоимость", "цена договора"]),
    ("paid_amount", ["оплач", "уплач", "платеж"]),
    ("debt_amount", ["задолжен", "долг", "остаток"]),
    ("penalty_amount", ["неустойк", "пен", "штраф"]),
    ("interest_amount", ["процент", "395"]),
    ("claim_amount", ["цена иска", "сумма иска", "итого"]),
    ("pretrial_date", ["претензи"]),
    ("court_name", ["суд"]),
    ("category", ["категор", "вид спора"]),
    ("deadline_date", ["срок"]),
]


def guess_roles(fields: dict) -> dict[str, list[str]]:
    roles: dict[str, list[str]] = {}
    for name in fields:
        low = name.lower()
        for role, hints in ROLE_HINTS:
            if any(hint in low for hint in hints):
                roles.setdefault(role, []).append(name)
    return roles


def grep_code(pattern: str, label: str) -> list[str]:
    regex = re.compile(pattern, re.I)
    hits: list[str] = []
    for path in walk_files({".py"}):
        try:
            lines = path.read_text("utf-8", "ignore").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines, 1):
            if regex.search(line):
                hits.append(
                    f"{path.relative_to(ROOT)}:{index}: {line.strip()[:120]}"
                )
                if len(hits) > 40:
                    return hits
    return hits


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    samples = find_passport_samples()
    schema = infer_schema(samples) if samples else {}
    roles = guess_roles(schema)
    contract = {
        "samples": [str(path.relative_to(ROOT)) for path, _ in samples],
        "fields": {
            key: {
                "types": value["types"],
                "required": value["required"],
                "examples": value["examples"],
            }
            for key, value in schema.items()
        },
        "role_candidates": roles,
        "note": "field_map.json заполняется по этим данным; роли без кандидата — вручную",
    }
    (OUT / "passport_contract.json").write_text(
        json.dumps(contract, ensure_ascii=False, indent=1), "utf-8"
    )
    validators = grep_code(
        r"def\s+\w*(valid|check|verify)\w*\s*\(.*(паспорт|passport|schema)",
        "validators",
    )
    generator = grep_code(
        r"(исков|claim).{0,40}(generat|build|docx|формир)", "generator"
    )
    markdown = [
        "# ARCHITECTURE_AUDIT (сгенерировано passport_audit.py)",
        "",
        f"- Корень: {ROOT}",
        f"- Образцов паспорта найдено: {len(samples)}",
        "",
    ]
    markdown.extend(
        f"- образец: `{path.relative_to(ROOT)}`" for path, _ in samples
    )
    markdown += [
        "",
        "## Поля (выведено из образцов)",
        "",
        "| Поле | Типы | Обяз. | Пример |",
        "|---|---|---|---|",
    ]
    for key, value in schema.items():
        markdown.append(
            f"| {key} | {','.join(value['types'])} | "
            f"{'да' if value['required'] else 'нет'} | "
            f"{'; '.join(value['examples'])[:80]} |"
        )
    markdown += [
        "",
        "## Кандидаты в валидаторы паспорта",
        "```",
        *(validators or ["НЕ НАЙДЕНЫ — указать в field_map.json"]),
        "```",
        "",
        "## Кандидаты в генератор иска",
        "```",
        *(generator or ["НЕ НАЙДЕНЫ"]),
        "```",
        "",
        "## Далее (делает человек/OpenCode)",
        "1. Проверить таблицы выше против реального кода.",
        "2. Заполнить `artifacts/passport/field_map.json`.",
        "3. Вписать путь к валидатору в ключ `validator` (import:func).",
        "4. Запустить тесты: `pytest backend/tests/test_passport_ai.py -q`.",
    ]
    (OUT / "ARCHITECTURE_AUDIT.md").write_text(
        "\n".join(markdown) + "\n", "utf-8"
    )
    template = {
        "validator": "",
        "claim_generator": "",
        "fields": {
            role: {
                "pointer": f"/{candidates[0]}",
                "kind": "text",
                "required": False,
            }
            for role, candidates in roles.items()
            if candidates
        },
    }
    field_map_path = OUT / "field_map.json"
    if not field_map_path.exists():
        field_map_path.write_text(
            json.dumps(template, ensure_ascii=False, indent=1), "utf-8"
        )
    print(
        f"OK: {OUT / 'ARCHITECTURE_AUDIT.md'}; "
        f"образцов={len(samples)}; ролей-кандидатов={len(roles)}"
    )


if __name__ == "__main__":
    main()
