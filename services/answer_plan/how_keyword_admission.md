### OBJECTIVE

Answer **HOW-KEYWORD-ADMISSION** questions with context-first guidance while keeping policy facts authoritative.

### CANONICAL INPUT (TARGET CONTRACT)

- Same target contract as `how_admission`: `answer.kind = "procedure"`.
- Contextual enrichment may appear in `DATA_CRAWLED`, but policy facts must come from `PRIMARY_FACTS`.

### TRANSITIONAL COMPATIBILITY (CURRENT STATE)

- Reuse mapping from `how_admission`:
  - `admission_policy` -> `answer.data.policy`
  - `admission_methods` -> `answer.data.methods`
  - `admission_combinations` -> `answer.data.combinations`
  - `major_quotas_scores` -> `answer.data.competitiveness`

### RESPONSE FLOW

1. CONTEXT OPENING (OPTIONAL)
- Use brief non-authoritative context from `DATA_CRAWLED`.

2. METHOD & POLICY CORE
- Use policy/method evidence as the main body.

3. EXECUTION STEPS
- Provide concrete portal/procedure/hotline guidance.

### CONSTRAINTS

- `PRIMARY_FACTS` overrides contextual data.
- No GDU-specific facts from crawled context.

### MANDATORY FIELD COVERAGE & NON-FABRICATION

- Treat every non-empty field in `PRIMARY_FACTS` as mandatory evidence input.
- Mandatory utilization order:
  1. `PRIMARY_FACTS.answer.data.*` (highest priority)
  2. `PRIMARY_FACTS.answer.kind`, `PRIMARY_FACTS.intent`, `PRIMARY_FACTS.status`
  3. `PRIMARY_FACTS.meta.*`
  4. `PRIMARY_FACTS.evidence.*`
  5. `PRIMARY_FACTS.pagination.*`
- Do not skip any non-empty field. If a field is not directly answerable for the current user wording, acknowledge it briefly and keep it available for clarification.
- In final response, ensure every non-empty field in `PRIMARY_FACTS.answer.data` is either:
  - directly used as a fact, or
  - explicitly marked as unavailable/insufficient for the requested detail.
- If query results are insufficient, off-topic, mismatched, or wrong for the user's question:
  - politely refuse unsupported claims,
  - state which data is missing/mismatched,
  - guide the user to restate target entity/scope or requested attribute.
- Never fabricate facts, numbers, names, timelines, or policies.
- If both structured and fallback data cannot support the asked detail, return a concise non-fabricated refusal with guidance.
