### OBJECTIVE

Answer **HOW-KEYWORD-PROCEDURE-POLICY** questions with policy-first guidance on the official process or procedure only.

### CANONICAL INPUT (TARGET CONTRACT)

- Same target contract as `how_procedure_policy`: `answer.kind = "procedure"`.

### TRANSITIONAL COMPATIBILITY (CURRENT STATE)

- `policy_overview` -> `answer.data.policy`
- `policy_process` -> `answer.data.process`

### RESPONSE FLOW

1. Policy identity.
2. Official process.

### CONSTRAINTS

- Do not invent procedural steps or sanctions.
- Keep the policy record as the only source of truth.

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
