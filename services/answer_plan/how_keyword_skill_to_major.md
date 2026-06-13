### OBJECTIVE

Answer **HOW-KEYWORD-SKILL-TO-MAJOR** questions with direct skill-to-major recommendations.

### CANONICAL INPUT (TARGET CONTRACT)

- Same target contract as `how_skill_to_major`: `answer.kind = "eligibility"`.

### TRANSITIONAL COMPATIBILITY (CURRENT STATE)

- `related_majors` -> `answer.data.recommended_majors`

### RESPONSE FLOW

1. Confirm skill context.
2. Recommend matching majors.
3. Explain fit from major descriptions.

### CONSTRAINTS

- No fabricated ranking/fit score.
- Keep recommendations limited to retrieved majors.

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
