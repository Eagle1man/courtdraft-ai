# DATA_FLOW — путь данных паспорт-ИИ

```text
CaseDocument + DocumentChunk (БД)
        │
        ▼
reader.from_db_documents()
  DocIntake + Fragment{locator,text}
        │
        ▼
classify.classify_doc()
  DocClass{doc_type,confidence,reasons}
        │
        ▼
extract.extract_facts_llm() + extract_facts_det()
  FactCandidate{role,value,quote,locator,status}
        │
        ├── quote-gate: цитата есть в документе
        ├── value-gate: цитата подтверждает значение
        └── UNSUPPORTED: факт виден, но не принимается
        │
        ▼
reconcile.reconcile() + arithmetic_checks()
  chosen / Conflict / ValidationFinding
        │
        ▼
assembler.assemble()
  draft JSON + FieldDraft[]
  · Decimal → int копеек
  · ISO → ДД.ММ.ГГГГ
  · claims/events/documents по derived-правилам
        │
        ▼
validators.validate_existing()
  CasePassport.model_validate + validate_invariants
        │
        ▼
человек: confirmed / edited / rejected
        │
        ▼
service.confirm() → router.confirm_passport_ai()
  бэкап → CasePassportRepository.upsert → commit
        │
        ▼
существующий generate_claim_draft() → DOCX
```

## Промежуточные файлы

Каталог: `data/cases/<case_id>/passport_ai/`.

- `intake.json` — реестр документов;
- `frag_<document_id>.json` — фрагменты с локаторами;
- `classes.json` — классификация;
- `facts.json` — все кандидаты, включая UNSUPPORTED;
- `conflicts.json` — конфликты;
- `draft_passport.json` — проект паспорта;
- `fields.json` — статусы полей и действия человека;
- `validation.json` — результаты существующего валидатора;
- `state.json` — этап и время;
- `backups/` — бэкапы подтверждённых паспортов.
