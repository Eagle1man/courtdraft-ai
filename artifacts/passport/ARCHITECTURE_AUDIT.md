# ARCHITECTURE_AUDIT (сгенерировано passport_audit.py)

- Корень: D:\courtdraft-ai
- Образцов паспорта найдено: 1

- образец: `backend\tests\fixtures\case_2-101-2025\00-ПАСПОРТ-ДЕЛА.json`

## Поля (выведено из образцов)

| Поле | Типы | Обяз. | Пример |
|---|---|---|---|
| schema | string | да | case-passport/3.1 |
| mode | string | да | ETALON |
| case | object | да | {'number': '2-101/2025', 'court_for_header': 'Мировому судье судебного участка № |
| parties | object | да | {'plaintiff': {'full': 'Смирнова Ольга Викторовна', 'short': 'Смирнова О.В.', 'g |
| money_kopecks | object | да | {'loan': 10000000, 'repaid': 1000000, 'principal': 9000000, 'interest': 1026329, |
| interest | object | да | {'norm': 'ст. 395 ГК РФ', 'from': '11.08.2024', 'to': '05.03.2025', 'days': 207, |
| claims | list | да | [{'n': 1, 'kind': 'principal', 'amount_kopecks': 9000000, 'text': 'задолженность |
| contract | object | да | {'number': 'б/н', 'date': '10.02.2024', 'due_date': '01.08.2024', 'interest_free |
| events | list | да | [{'date': '10.02.2024', 'code': 'CONTRACT', 'fact': 'заключён договор займа б/н, |
| suit_date | date | да | 22.01.2025 |
| fee_receipt | object | да | {'id': 'чек-ордер № 000123', 'date': '20.01.2025', 'amount_kopecks': 290000} |
| pretrial | object | да | {'letter_id': 'исх. № 1', 'letter_date': '20.12.2024', 'rpo': '10500012345678',  |
| suit_rpo | money? | да | 10500012345912 |
| law | object | да | {'material': ['ст. 309 ГК РФ', 'ст. 310 ГК РФ', 'ст. 807 ГК РФ', 'ст. 808 ГК РФ' |
| documents | list | да | [{'file': '01-Карточка-сторон.md', 'title': 'Карточка сторон и подсудности', 'sh |
| gaps | list | да | ['адрес истца', 'адрес ответчика', 'телефон истца', 'паспорт истца', 'паспорт от |

## Кандидаты в валидаторы паспорта
```
scripts\kaggle\train_qlora_sft.py:126: def validate_instance(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
kaggle\stage_sft_facts\train_qlora_sft.py:126: def validate_instance(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
backend\app\harnesses\structured_output.py:84: def validate_value(value: Any, schema: dict[str, Any], *, validator: Callable[[Any], Sequence[str]] | None = None) -> li
backend\app\ingestion\passport.py:219: def _check_schema(self) -> CasePassport:
backend\app\ingestion\passport.py:225: def validate_invariants(self) -> list[PassportIssue]:
```

## Кандидаты в генератор иска
```
scripts\passport_audit.py:106: generator = grep_code(r"(исков|claim).{0,40}(generat|build|docx|формир)", "generator")
scripts\passport_audit.py:122: tmpl = {"validator": "", "claim_generator": "",
backend\tests\test_civil_claim.py:13: from app.drafting.claim_builder import ClaimBuildInput, build_claim
backend\tests\test_civil_claim.py:14: from app.drafting.claim_draft import generate_claim_draft, verify_claim_draft
backend\tests\test_civil_claim.py:196: result = build_claim(ClaimBuildInput.from_dict(_loan_data()))
backend\tests\test_civil_claim.py:212: result = build_claim(ClaimBuildInput.from_dict({}))
backend\tests\test_civil_claim.py:224: result = build_claim(ClaimBuildInput.from_dict(_loan_data()))
backend\tests\test_civil_claim.py:231: r1 = build_claim(ClaimBuildInput.from_dict({"demands": [{"amount": 100000}]}))
backend\tests\test_civil_claim.py:232: r2 = build_claim(ClaimBuildInput.from_dict({"demands": [{"amount": 100001}]}))
backend\tests\test_civil_claim.py:233: r3 = build_claim(ClaimBuildInput.from_dict({"demands": [{"amount": 100100}]}))
backend\tests\test_civil_claim.py:234: r4 = build_claim(ClaimBuildInput.from_dict({"demands": [{"amount": 200001}]}))
backend\tests\test_civil_claim.py:243: ok_50 = build_claim(ClaimBuildInput.from_dict({
backend\tests\test_civil_claim.py:250: warn_50001 = build_claim(ClaimBuildInput.from_dict({
backend\tests\test_civil_claim.py:264: ok_100 = build_claim(ClaimBuildInput.from_dict({
backend\tests\test_civil_claim.py:271: warn_100001 = build_claim(ClaimBuildInput.from_dict({
backend\tests\test_civil_claim.py:285: result = build_claim(ClaimBuildInput.from_dict({
backend\tests\test_civil_claim.py:301: class TestClaimGenerationPhase1:
backend\tests\test_civil_claim.py:308: built = build_claim(ClaimBuildInput.from_dict(data))
backend\tests\test_claims.py:12: from app.verification.claims import build_claims, check_completeness, split_claims
backend\tests\test_claims.py:35: claims = build_claims(
backend\tests\test_claims.py:43: assert claim.evidence[0].document == "иск.docx"
backend\tests\test_claims.py:47: claims = build_claims(
backend\tests\test_claims.py:57: claims = build_claims(
backend\tests\test_claims.py:64: claims = build_claims(
backend\tests\test_claim_bases.py:17: from app.drafting.operative import MoneyClaim, Outcome, build_operative
backend\tests\test_fee_to_budget.py:19: from app.drafting.operative import REVIEW, MoneyClaim, build_operative
backend\tests\test_laws.py:20: from app.drafting.operative import MoneyClaim, build_operative
backend\tests\test_non_money_claims.py:18: from app.drafting.operative import Claimant, Outcome, build_operative
backend\app\agents\draft_agent.py:228: # Латиница в именах — обычное дело (claim.pdf, dogovor.docx). Без неё
backend\app\agents\draft_agent.py:825: нормально, а «Возражение на исковое заявление.docx: «…»» — как
backend\app\agents\draft_agent.py:854: # причине: файлы часто называют claim.pdf / statement.docx, и
backend\app\drafting\claim_builder.py:178: class ClaimBuildInput:
backend\app\drafting\claim_builder.py:196: def from_dict(cls, data: dict[str, Any]) -> "ClaimBuildInput":
backend\app\drafting\claim_builder.py:239: def _jurisdiction_check(data: ClaimBuildInput, price: Decimal) -> dict[str, Any]:
backend\app\drafting\claim_builder.py:281: def _limitation_check(data: ClaimBuildInput) -> dict[str, Any] | None:
backend\app\drafting\claim_builder.py:297: def _pretrial_check(data: ClaimBuildInput) -> dict[str, Any]:
backend\app\drafting\claim_builder.py:320: def _attachments_check(data: ClaimBuildInput, demands: list[ClaimDemandInput], price: Decimal) -> tuple[list[dict[str, A
backend\app\drafting\claim_builder.py:355: def build_claim(data: ClaimBuildInput) -> dict[str, Any]:
backend\app\drafting\claim_draft.py:28: from app.drafting.claim_builder import ClaimBuildInput, build_claim
backend\app\drafting\claim_draft.py:59: def _claim_input_from_context(claim_data: dict[str, Any] | None, case: Any = None) -> ClaimBuildInput:
backend\app\drafting\claim_draft.py:66: return ClaimBuildInput.from_dict(data)
```

## Далее (делает человек/OpenCode)
1. Проверить таблицы выше против реального кода.
2. Заполнить `artifacts/passport/field_map.json` (шаблон ниже).
3. Вписать путь к валидатору в ключ `validator` (import:func) или оставить пустым.
4. Запустить тесты: `pytest backend/tests/test_passport_ai.py -q`.

## Сверка с реальным контрактом (обязательный гейт S1)

Реальный образец: `backend/tests/fixtures/case_2-101-2025/00-ПАСПОРТ-ДЕЛА.json`.

Выявленные расхождения с предположениями поставки:

1. Паспорт вложенный: `case`, `parties`, `money_kopecks`, `interest`, `contract`,
   `pretrial`, `fee_receipt`, `claims`, `events`, `documents`. Плоский field_map
   из поставки неприменим.
2. Деньги хранятся целыми копейками (`int`), а не строками/Decimal.
   Внутренний расчёт — Decimal (рубли), на границе паспорта — `int(kopecks)`.
3. Даты хранятся в формате `ДД.ММ.ГГГГ`; внутренняя нормализация — ISO.
4. `claims`, `events`, `documents` — массивы. Они собираются детерминированно
   по правилам `derived` в `field_map.json`, а не выдумываются LLM.
5. Реальный валидатор: `app.ingestion.passport.CasePassport` +
   `validate_invariants()`. Адаптер: `app.passport_ai.validators:validate_existing`.
6. Документы уже находятся в БД (`CaseDocument` + `DocumentChunk`); новый модуль
   читает их оттуда и не запускает OCR/PDF/DOCX-парсеры повторно.

`field_map.json` заполнен до написания кода. Любые дальнейшие изменения контракта
запрещены без отдельного обоснования.

