### OBJECTIVE

Answer **HOW-KEYWORD-STUDENT-LIFE** questions with join-now guidance for clubs and activities.

### CANONICAL INPUT (TARGET CONTRACT)

- Same target contract as `how_student_life`: `answer.kind = "procedure"`.

### TRANSITIONAL COMPATIBILITY (CURRENT STATE)

- `clubs_overview` -> `answer.data.clubs`
- `activities` -> `answer.data.activities`
- `support_services_detail` -> `answer.data.support_services`

### RESPONSE FLOW

1. Club entry channel.
2. Activity timing and participation.
3. Support backup.

### CONSTRAINTS

- Keep links/dates exact when present.
- No fabricated registration windows or event schedules.

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
