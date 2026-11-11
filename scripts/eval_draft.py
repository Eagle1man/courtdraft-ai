"""Линейка измерений: четыре числа на отложенных делах.

Замер нужен не для красоты. Без него любое изменение модели или правил
нельзя оценить — остаётся спорить о том, стало лучше или хуже. Числа
фиксируются до изменения и после него, и сравниваются.

Что измеряется на каждом деле из data/lora/eval.jsonl:

1. Доля пригодных разделов — какая часть текста, написанного моделью,
   годится вообще. Проверка та же, что в приложении
   (drafting/section_quality): кириллица, отсутствие обрывов мысли,
   отсутствие повторов.
2. Числа без источника — сколько чисел в проекте не подтверждаются
   материалами дела. Это продуктовая норма: число без источника
   считается выдуманным. Считает проверка (verification/engine.py).
3. Сходимость итогов — в скольких делах проверка не нашла ни одного
   HIGH или CRITICAL по неподтверждённым утверждениям.
4. Доля процедурных отписок — сколько разделов отвечают отпиской
   вместо текста.

Разделы, собранные по правилам (резолютивная часть), в первом и
четвёртом числе не участвуют: их пишет не модель, и оценивать
пригодность её текста там нечего.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
CASES_DIR = ROOT / "data" / "lora" / "eval_cases"
REPORT_FILE = ROOT / "data" / "lora" / "eval_report.json"
WORKDIR = ROOT / "data" / "lora" / "_eval_run"

sys.path.insert(0, str(BACKEND))

from app.agents.draft_agent import DraftResult, generate_draft  # noqa: E402
from app.agents.evidence_pack import build_evidence_pack, load_template  # noqa: E402
from app.config import (  # noqa: E402
    EmbeddingProviderName,
    LLMProviderName,
    Settings,
)
from app.drafting.section_quality import unusable_reason  # noqa: E402
from app.ingestion.extract import guess_document_type  # noqa: E402
from app.models.entities import CaseDocument  # noqa: E402
from app.verification.engine import (  # noqa: E402
    IssueType,
    Severity,
    verify_draft,
)

REFERENCE_FILE = "court_reference.txt"

#: Отписка вместо текста. Считается отдельно от пригодности: раздел
#: может быть формально пригодным и при этом ничего не говорить.
PROCEDURAL = (
    "раздел не сформирован",
    "и считаю возменным",
    "иск оставить без рассмотрения",
    "недостаточно сведений",
)


@dataclass
class CaseResult:
    """Что получилось по одному делу."""

    case_number: str
    sections_total: int = 0
    sections_model: int = 0
    sections_usable: int = 0
    sections_procedural: int = 0
    claims_checked: int = 0
    unsupported_claims: int = 0
    severe_issues: int = 0
    needs_review: int = 0
    operative_numbers: int = 0
    operative_numbers_backed: int = 0
    court_numbers: int = 0
    court_numbers_found: int = 0
    issues_by_type: dict[str, int] = field(default_factory=dict)
    issues_by_severity: dict[str, int] = field(default_factory=dict)
    error: str = ""
    notes: list[str] = field(default_factory=list)


def make_settings(provider: str, workdir: Path) -> Settings:
    """Настройки прогона. База во временном каталоге, эмбеддинги —
    заглушка: извлечение измерение не проверяет, а настоящая модель
    эмбеддингов отбирала бы память у модели, которая пишет текст."""
    mock = provider == "mock"
    workdir.mkdir(parents=True, exist_ok=True)
    return Settings(
        app_env="development",
        llm_provider=LLMProviderName.MOCK if mock else LLMProviderName.OLLAMA,
        embedding_provider=EmbeddingProviderName.MOCK if mock else EmbeddingProviderName.LOCAL,
        ollama_enabled=not mock,
        ollama_model="" if mock else _model_from_env(),
        ollama_base_url="http://127.0.0.1:11434",
        ollama_timeout_sec=1800,
        ollama_num_ctx=2048,
        embedding_batch_size=8,
        database_url=f"sqlite+aiosqlite:///{(workdir / 'eval.db').as_posix()}",
        storage_dir=workdir,
        documents_dir=workdir / "documents",
        laws_dir=ROOT / "data" / "laws",
        templates_dir=ROOT / "data" / "templates",
        chunk_size=800,
        chunk_overlap=120,
    )


def _model_from_env() -> str:
    """Та же модель, что в приложении: из .env, а не зашитая."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return "qvikhr:latest"
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("OLLAMA_MODEL="):
            value = stripped.split("=", 1)[1].strip().strip('"')
            if value:
                return value
    return "qvikhr:latest"


def document_for(filename: str, text: str) -> CaseDocument:
    """Один файл материала дела как документ.

    Тип определяется по имени файла тем же кодом, что и в приложении:
    от него зависит, в какую ветку разбора попадёт документ и какие
    факты из него извлекутся. Свои правила здесь означали бы, что замер
    идёт по другой системе, чем работает приложение.
    """
    return CaseDocument(
        id=uuid.uuid4(),
        case_id=uuid.uuid4(),
        filename=filename,
        document_type=guess_document_type(filename, text),
        mime_type="text/plain",
        page_count=1,
        text=text,
        metadata_json={},
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def load_case(case_dir: Path) -> tuple[str, list[CaseDocument], str]:
    """Материалы дела, эталонная резолюция и их общий текст."""
    documents: list[CaseDocument] = []
    joined: list[str] = []
    for path in sorted(case_dir.glob("*.txt")):
        if path.name == REFERENCE_FILE:
            continue
        # Судебный акт — не материал дела. В eval он давал утечку
        # исхода: модель могла «угадать» решение, прочитав его.
        if "court_findings" in path.name.lower():
            continue
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            continue
        documents.append(document_for(path.name, text))
        joined.append(text)
    reference_path = case_dir / REFERENCE_FILE
    reference = (
        reference_path.read_text(encoding="utf-8").strip() if reference_path.exists() else ""
    )
    return "\n\n".join(joined), documents, reference


def numbers_in(text: str) -> set[int]:
    """Числа текста в целых единицах — для проверки покрытия."""
    import re

    found: set[int] = set()
    for raw in re.findall(r"\d[\d\s]{0,15}(?:,\d{1,2})?", text):
        digits = re.sub(r"[^\d,]", "", raw)
        whole, _, frac = digits.partition(",")
        try:
            value = int(whole or 0) * 100 + int((frac or "0").ljust(2, "0")[:2])
        except ValueError:
            continue
        if value:
            found.add(value)
    return found


def operative_of(draft: DraftResult) -> str:
    """Резолютивная часть проекта."""
    for section in draft.sections:
        if "резолютив" in section.name.lower():
            return section.text
    return ""


def assess(
    draft: DraftResult,
    pack: Any,
    case_number: str,
    materials: str,
    reference: str,
) -> CaseResult:
    """Посчитать четыре числа по одному делу."""
    result = CaseResult(case_number=case_number)
    materials_numbers = numbers_in(materials)

    for section in draft.sections:
        text = section.text or ""
        result.sections_total += 1
        if "резолютив" in section.name.lower():
            continue
        result.sections_model += 1
        reason = unusable_reason(text)
        if reason:
            result.notes.append(f"{section.name}: непригоден — {reason[:80]}")
        else:
            result.sections_usable += 1
        if any(marker in text.lower() for marker in PROCEDURAL):
            result.sections_procedural += 1

    operative = operative_of(draft)
    operative_numbers = numbers_in(operative)
    result.operative_numbers = len(operative_numbers)
    result.operative_numbers_backed = len(operative_numbers & materials_numbers)

    # Сколько сумм из реального решения суда нашлось в нашей резолюции.
    # Это честная мера полноты: суд постановил эти суммы в том же деле,
    # материалы которого лежат в папке.
    court_numbers = numbers_in(reference)
    result.court_numbers = len(court_numbers)
    result.court_numbers_found = len(court_numbers & operative_numbers)

    report = verify_draft(draft, pack)
    result.claims_checked = report.claims_checked
    result.unsupported_claims = report.unsupported_claims
    for issue in report.issues:
        key = issue.type.value
        result.issues_by_type[key] = result.issues_by_type.get(key, 0) + 1
        grade = issue.severity.value
        result.issues_by_severity[grade] = result.issues_by_severity.get(grade, 0) + 1
        if issue.severity in (Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL):
            result.needs_review += 1
        if issue.severity in (Severity.HIGH, Severity.CRITICAL):
            result.severe_issues += 1
            if issue.type is IssueType.UNSUPPORTED_CLAIM:
                result.notes.append(f"замечание: {issue.text[:100]}")
    return result


async def run(provider: str, limit: int, only_sections: list[str] | None) -> int:
    if not CASES_DIR.exists():
        print(f"Нет каталога {CASES_DIR}. Сначала соберите выборку:")
        print("  python scripts/build_lora_dataset.py")
        return 1

    settings = make_settings(provider, WORKDIR)
    template = load_template(settings)
    cases = sorted(item for item in CASES_DIR.glob("case-*") if item.is_dir())
    if limit:
        cases = cases[:limit]
    print(f"дел в замере: {len(cases)}")
    print(f"провайдер: {provider}, модель: {settings.ollama_model}\n")

    results: list[CaseResult] = []
    for number, case_dir in enumerate(cases, start=1):
        case_number = f"EVAL-{number:03d}"
        try:
            joined, documents, reference = load_case(case_dir)
            pack = build_evidence_pack(
                case_number,
                "Суд отложенного замера",
                "договорные споры",
                documents,
                settings=settings,
            )
            draft = await generate_draft(
                pack, template, settings=settings, only_sections=only_sections
            )
            outcome = assess(draft, pack, case_number, joined, reference)
        except Exception as exc:  # noqa: BLE001
            outcome = CaseResult(case_number=case_number, error=f"{type(exc).__name__}: {exc}")
        results.append(outcome)

        flag = "!" if outcome.error or outcome.severe_issues else " "
        print(
            f"{flag} {case_number}: разделов {outcome.sections_model} "
            f"(пригодно {outcome.sections_usable}), "
            f"чисел без источника {outcome.unsupported_claims} из {outcome.claims_checked}, "
            f"сумм суда найдено {outcome.court_numbers_found} из {outcome.court_numbers}, "
            f"тяжёлых замечаний {outcome.severe_issues}"
            + (f" ОШИБКА: {outcome.error}" if outcome.error else "")
        )

    summary = summarise(results)
    REPORT_FILE.write_text(
        json.dumps(
            {
                "провайдер": provider,
                "модель": settings.ollama_model,
                "дел": len(results),
                "сводка": summary,
                "по_делам": [vars(item) for item in results],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("\nСВОДКА")
    for name, value in summary.items():
        print(f"  {name}: {value}")
    print(f"\nотчёт: {REPORT_FILE}")
    return 0


def summarise(results: list[CaseResult]) -> dict[str, Any]:
    """Свести четыре числа по всем делам."""
    ok = [item for item in results if not item.error]
    model_sections = sum(item.sections_model for item in ok)
    usable = sum(item.sections_usable for item in ok)
    procedural = sum(item.sections_procedural for item in ok)
    claims = sum(item.claims_checked for item in ok)
    unsupported = sum(item.unsupported_claims for item in ok)
    clean = sum(1 for item in ok if not item.severe_issues)
    operative_numbers = sum(item.operative_numbers for item in ok)
    operative_backed = sum(item.operative_numbers_backed for item in ok)
    court_numbers = sum(item.court_numbers for item in ok)
    court_found = sum(item.court_numbers_found for item in ok)

    by_type: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for item in ok:
        for key, count in item.issues_by_type.items():
            by_type[key] = by_type.get(key, 0) + count
        for key, count in item.issues_by_severity.items():
            by_severity[key] = by_severity.get(key, 0) + count

    return {
        "дел_без_ошибок": len(ok),
        "дел_с_ошибками": len(results) - len(ok),
        "разделов_от_модели": model_sections,
        "1_пригодных_разделов": f"{usable}/{model_sections}",
        "1_доля_пригодных": f"{usable / model_sections:.1%}" if model_sections else "—",
        "2_чисел_без_источника": unsupported,
        "2_из_проверенных": claims,
        "3_дел_без_тяжёлых_замечаний": f"{clean}/{len(ok)}",
        "4_процедурных_отписок": f"{procedural}/{model_sections}",
        "замечания_по_типу": by_type,
        "замечания_по_важности": by_severity,
        "числа_в_резолюции": operative_numbers,
        "числа_в_резолюции_из_материалов": operative_backed,
        "сумм_в_решении_суда": court_numbers,
        "сумм_суда_найдено_системой": court_found,
        "полнота_по_суммам_суда": (
            f"{court_found / court_numbers:.1%}" if court_numbers else "—"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default="ollama", choices=["ollama", "mock"])
    parser.add_argument("--limit", type=int, default=0, help="0 — все отложенные дела")
    parser.add_argument("--only-sections", nargs="*", default=None)
    args = parser.parse_args(argv)
    return asyncio.run(run(args.provider, args.limit, args.only_sections))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
