### OBJECTIVE

Answer **HOW-KEYWORD-FACILITIES** questions with direct usage guidance and support routing.

### CANONICAL INPUT (TARGET CONTRACT)

- Same target contract as `how_facilities`: `answer.kind = "procedure"`.

### TRANSITIONAL COMPATIBILITY (CURRENT STATE)

- `facility_usage` -> `answer.data.facility_usage`
- `managed_facilities` -> `answer.data.manager_contacts`

### RESPONSE FLOW

1. Facility overview and usage intent.
2. Actionable usage direction.
3. Manager/support handoff.

### CONSTRAINTS

- No fabricated operating rules or schedules.
- Keep support path explicit and official.

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
